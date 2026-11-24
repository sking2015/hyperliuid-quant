import asyncio
import logging
import math
import os
import shutil
from datetime import timedelta

import httpx
from psycopg.types.json import Jsonb

from app.config import BOOTSTRAP_BARS, DATA_DIR, HISTORY_INTERVALS, MINUTES, SNAPSHOT_SECONDS
from app.exchange.hyperliquid.public import PublicClient, candle, outcome_rows, perp_rows, spot_rows, timestamp
from app.market_data.capitalization import load_caps
from app.market_data.funding import funding_loop
from app.market_data.import_jobs import import_loop
from app.market_data.repository import catalog, now, record_gaps, save_candles
from app.market_data.stream import stream_loop
from app.storage.db import connect, migrate
from app.watchlist.service import refresh

log = logging.getLogger(__name__)


def save_scope(kind, dex, rows, stamp, caps):
    for asset, metrics in rows:
        metrics.update(caps.get(asset["instrument_id"], {}))
    with connect() as db:
        catalog(db, kind, dex, rows, stamp)


async def snapshot_once(client, state):
    try:
        caps = await asyncio.to_thread(load_caps, now())
        state["market_cap_error"] = None
    except (ValueError, TypeError, KeyError, OSError, ArithmeticError, AttributeError) as exc:
        caps = {}
        state["market_cap_error"] = str(exc)[:300]
    meta, contexts = await client.info({"type": "spotMetaAndAssetCtxs"})
    tokens, rows = spot_rows(meta, contexts)
    await asyncio.to_thread(save_scope, "SPOT", "", rows, now(), caps)
    dexes = await client.info({"type": "perpDexs"})
    errors = []
    for index, details in enumerate(dexes):
        if details is None and index != 0:
            continue
        dex = details["name"] if details else ""
        try:
            meta, contexts = await client.info({"type": "metaAndAssetCtxs", "dex": dex})
            rows = perp_rows(meta, contexts, tokens, dex, index)
            await asyncio.to_thread(save_scope, "HIP3" if dex else "PERP", dex, rows, now(), caps)
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            errors.append(f"{dex or 'PERP'}: {exc}")
    try:
        rows = outcome_rows(await client.info({"type": "outcomeMeta"}))
        await asyncio.to_thread(save_scope, "OUTCOME", "", rows, now(), caps)
    except (ValueError, KeyError, httpx.HTTPError) as exc:
        errors.append(f"OUTCOME: {exc}")
    state["snapshot_error"] = "; ".join(errors)[:1000] or None
    state["snapshot_at"] = now().isoformat()


async def snapshots(client, state):
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        try:
            await snapshot_once(client, state)
        except Exception as exc:
            state["snapshot_error"] = str(exc)[:500]
            log.exception("snapshot refresh failed")
        await asyncio.sleep(max(1, SNAPSHOT_SECONDS - (loop.time() - start)))


def next_history_job(priority):
    with connect() as db:
        db.execute("""
            INSERT INTO history_jobs(instrument_id,interval)
            SELECT instrument_id, intervals.id FROM instruments CROSS JOIN unnest(%s::text[]) intervals(id)
            WHERE status='ACTIVE' AND market_type<>'OUTCOME' ON CONFLICT DO NOTHING
        """, (list(HISTORY_INTERVALS),))
        return db.execute("""
            SELECT h.*, i.asset_key FROM history_jobs h JOIN instruments i USING (instrument_id)
            WHERE h.next_due <= now() AND i.status='ACTIVE'
            ORDER BY CASE WHEN %s AND EXISTS (
                SELECT 1 FROM watchlist_members m JOIN watchlists w ON w.id=m.watchlist_id
                WHERE m.instrument_id=i.instrument_id AND w.enabled AND w.deleted_at IS NULL
                  AND NOT m.blocked AND (m.manual OR m.pinned OR m.automatic)
            ) THEN CASE h.interval WHEN '1m' THEN 0 ELSE 1 END ELSE 2 END, h.next_due,
            CASE h.interval WHEN '1m' THEN 0 ELSE 1 END, i.instrument_id, h.interval LIMIT 1
        """, (priority,)).fetchone()


def complete_job(job, bars, start, end, success):
    with connect() as db:
        if success:
            if job["progress_ts"] and job["progress_ts"] < start:
                db.execute("""
                    INSERT INTO data_gaps VALUES (%s,%s,%s,%s,'outside_rest_retention',false)
                    ON CONFLICT DO NOTHING
                """, (job["instrument_id"], job["interval"], job["progress_ts"], start))
            save_candles(db, bars)
            record_gaps(db, job["instrument_id"], job["interval"], start, end)
        seconds = max(60, MINUTES[job["interval"]] * 60) if success else 120
        db.execute("""
            UPDATE history_jobs SET next_due=now()+(%s*interval '1 second'),
              progress_ts=CASE WHEN %s THEN %s ELSE progress_ts END
            WHERE instrument_id=%s AND interval=%s
        """, (seconds, success, end, job["instrument_id"], job["interval"]))


async def history_once(client, job):
    interval = job["interval"]
    step = MINUTES[interval] * 60_000
    stamp = now()
    end = int(stamp.timestamp() * 1000) // step * step
    progress = job["progress_ts"]
    start = max(end - step * (BOOTSTRAP_BARS if progress is None else 4999),
                int(progress.timestamp() * 1000) - 2 * step if progress else 0)
    try:
        items = await client.info({"type": "candleSnapshot", "req": {
            "coin": job["asset_key"], "interval": interval, "startTime": start, "endTime": end - 1
        }}, 20 + math.ceil((end - start) / step / 60))
        bars = [candle(item, job["instrument_id"], stamp, "rest") for item in items
                if item["s"] == job["asset_key"] and item["i"] == interval and start <= item["t"] < end]
        await asyncio.to_thread(complete_job, job, bars, timestamp(start), timestamp(end), True)
    except Exception:
        await asyncio.to_thread(complete_job, job, [], timestamp(start), timestamp(end), False)
        raise


async def history_loop(client, state):
    turn = 0
    while True:
        try:
            job = await asyncio.to_thread(next_history_job, turn % 3 != 0)
            turn += 1
            if job:
                await history_once(client, job)
                state["history_error"] = None
        except Exception as exc:
            state["history_error"] = str(exc)[:250]
            log.exception("candle download failed")
        await asyncio.sleep(60 / 22)


def maintain(state):
    with connect() as db:
        pools = db.execute("""
            SELECT * FROM watchlists WHERE deleted_at IS NULL AND enabled AND type<>'MANUAL'
            AND (last_refresh IS NULL OR last_refresh + (config->>'refresh_seconds')::integer * interval '1 second' <= now())
            ORDER BY last_refresh NULLS FIRST LIMIT 10 FOR UPDATE SKIP LOCKED
        """)
        for pool in pools:
            refresh(db, pool, now())
        db.execute("""
            INSERT INTO worker_leases VALUES ('market',now(),%s) ON CONFLICT(component)
            DO UPDATE SET heartbeat=excluded.heartbeat,state=excluded.state
        """, (Jsonb({k: v for k, v in state.items() if not k.startswith("_")}),))


def data_file_bytes(root):
    def on_error(exc):
        raise exc

    total = 0
    hard_links = set()
    for directory, _, filenames in os.walk(root, onerror=on_error):
        for name in filenames:
            path = os.path.join(directory, name)
            try:
                if os.path.islink(path):
                    continue
                info = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                # Archive files may be renamed while this sample is taken.
                continue
            if info.st_nlink > 1:
                identity = (info.st_dev, info.st_ino)
                if identity in hard_links:
                    continue
                hard_links.add(identity)
            blocks = getattr(info, "st_blocks", None)
            total += blocks * 512 if blocks is not None else info.st_size
    return total


def database_bytes():
    with connect() as db:
        return db.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()["bytes"]


async def storage_stats(state):
    while True:
        metrics = await asyncio.gather(
            asyncio.to_thread(database_bytes),
            asyncio.to_thread(data_file_bytes, DATA_DIR),
            return_exceptions=True,
        )
        for name, value in zip(("database_bytes", "data_file_bytes"), metrics):
            if isinstance(value, Exception):
                log.warning("Could not measure %s: %s", name, value)
                state[name] = None
            else:
                state[name] = value
        await asyncio.sleep(60)


async def maintenance(state):
    while True:
        try:
            state["disk_free_bytes"] = shutil.disk_usage(DATA_DIR).free
            await asyncio.to_thread(maintain, state.copy())
        except Exception:
            log.exception("market maintenance failed")
        await asyncio.sleep(5)


async def main():
    migrate()
    DATA_DIR.mkdir(exist_ok=True, parents=True)
    state = {"ws_connected": False}
    # Hold a session-scoped PostgreSQL lock so duplicate workers cannot double the IP budget.
    with connect() as lease:
        if not lease.execute("SELECT pg_try_advisory_lock(6022027) AS acquired").fetchone()["acquired"]:
            raise RuntimeError("market worker already running")
        lease.commit()
        async with httpx.AsyncClient(timeout=12, proxy=os.getenv("HL_HTTP_PROXY")) as http:
            client = PublicClient(http)
            await asyncio.gather(snapshots(client, state), history_loop(client, state), stream_loop(state),
                                 maintenance(state), storage_stats(state), funding_loop(client, state),
                                 import_loop(), check_lease(lease))


async def check_lease(lease):
    while True:
        await asyncio.sleep(10)
        lease.execute("SELECT 1")
        lease.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())

