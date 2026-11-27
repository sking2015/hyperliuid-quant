import asyncio
import hashlib
import json
import logging
import os
import random
from collections import deque

from psycopg.types.json import Jsonb
from uuid import uuid4

from websockets.asyncio.client import connect as websocket

from app.config import DATA_DIR
from app.exchange.hyperliquid.public import WS
from app.market_data.monitor import WARN_FREE, disk_status
from app.market_data.repository import now
from app.research.feed import CH, health_rows, normalize, persist
from app.storage.db import connect, migrate
from app.workers.market import data_file_bytes, database_bytes

log = logging.getLogger(__name__)
DEFAULT = "BTC,ETH,SOL,HYPE,DOGE,XRP,ADA,AVAX,LINK,SUI"


def universe():
    names = list(dict.fromkeys(os.getenv("RESEARCH_ASSETS", DEFAULT).upper().replace(" ", "").split(",")))[:20]
    with connect() as db:
        rows = db.execute("""
            SELECT asset_key,instrument_id FROM instruments WHERE market_type='PERP'
              AND status='ACTIVE' AND quote='USDC' AND asset_key=ANY(%s)
        """, (names,)).fetchall()
    return {r["asset_key"]: r["instrument_id"] for r in rows}


def space_available(blocked=False):
    metric = disk_status(database_bytes(), data_file_bytes(DATA_DIR))
    sizes = [metric["disk_free_bytes"],metric["quota_remaining_bytes"]]
    if metric["host_probe_free_bytes"] is not None:
        sizes.append(metric["host_probe_free_bytes"])
    margin = max(0,int(os.getenv("RESEARCH_RESUME_MIB","128")))*1024**2 if blocked else 0
    return metric["disk_alert"]=="ok" and min(sizes)>WARN_FREE+margin


def contexts(assets, connection, seen):
    with connect() as db:
        rows = db.execute("""
            SELECT DISTINCT ON (instrument_id) instrument_id,as_of,mark,oracle,open_interest,funding
            FROM market_snapshots WHERE instrument_id=ANY(%s)
            AND as_of > now()-interval '5 minutes' ORDER BY instrument_id,as_of DESC
        """, (list(assets.values()),)).fetchall()
    received = now()
    result = []
    for row in rows:
        iid, observed = row["instrument_id"],row["as_of"]
        if seen.get(iid) == observed:
            continue
        seen[iid] = observed
        key = hashlib.sha256(f"research.context:{iid}:{observed.isoformat()}".encode()).hexdigest()
        payload = {key:str(row[key]) if row[key] is not None else None
                   for key in ("mark","oracle","open_interest","funding")}
        payload["snapshot_as_of"] = observed.isoformat()
        result.append((key,iid,"research.context",None,received,Jsonb(payload),connection,Jsonb(["exchange_time_missing"])))
    return result


def finish_gap(gap):
    if not gap:
        return
    assets,start,reason = gap
    with connect() as db, db.cursor() as c:
        c.executemany("""
           INSERT INTO data_gaps VALUES (%s,%s,%s,now(),%s,false) ON CONFLICT DO NOTHING
        """, [(iid, stream,start,reason) for iid in assets.values() for stream in CH.values()])


async def collect():
    state = {"connected": False, "reconnects": 0, "late": 0, "out_of_order": 0, "received": 0,
             "unconfirmed_batch_rows":0,"received_by_stream":{},"buffer_high_water":0}
    delays = deque(maxlen=2048)
    last_times = {}
    delay = 1
    gap = None
    blocked = False
    seen_contexts = {}
    while True:
        assets = {}
        connection = uuid4()
        rows = []
        last_received = now()
        try:
            assets = await asyncio.to_thread(universe)
            blocked = not await asyncio.to_thread(space_available,blocked)
            if not assets or blocked:
                state.update(connected=False, error="storage_threshold" if blocked else "missing_universe")
                await asyncio.to_thread(persist, [], state.copy())
                await asyncio.sleep(15)
                continue
            async with websocket(WS, open_timeout=10, ping_interval=None, max_queue=32) as socket:
                for asset in sorted(assets):
                    for channel in CH:
                        await socket.send(json.dumps({"method":"subscribe", "subscription":{"type":channel,"coin":asset}}))
                        await asyncio.sleep(.04)
                loop = asyncio.get_running_loop()
                pulse = ping = flush = loop.time()
                probe = loop.time() + 30
                context_at = loop.time()
                acknowledged = set()
                topology = {(coin, channel) for coin in assets for channel in CH}
                last_packet = loop.time()
                while True:
                    tick = loop.time()
                    if tick >= probe:
                        if not await asyncio.to_thread(space_available):
                            blocked = True
                            raise RuntimeError("storage_threshold")
                        probe = tick + 30
                    if tick >= context_at:
                        rows.extend(await asyncio.to_thread(contexts,assets,connection,seen_contexts))
                        context_at = tick + 15
                    if tick >= ping:
                        await socket.send('{"method":"ping"}')
                        ping = tick + 25
                    try:
                        raw = await asyncio.wait_for(socket.recv(), .2)
                    except TimeoutError:
                        if tick - last_packet > 55:
                            raise TimeoutError("research websocket heartbeat")
                    else:
                        received = now()
                        last_packet = tick
                        last_received = received
                        delay = 1
                        msg = json.loads(raw)
                        channel = msg.get("channel")
                        if channel == "error":
                            raise RuntimeError(str(msg.get("data"))[:200])
                        if channel == "subscriptionResponse":
                            sub = msg.get("data", {}).get("subscription", {})
                            acknowledged.add((sub.get("coin"), sub.get("type")))
                        if channel in CH:
                            for row in normalize(msg, assets, received, connection):
                                rows.append(row)
                                state["received"] += 1
                                iid, stream, exchange = row[1:4]
                                stream_counts = state["received_by_stream"]
                                stream_counts[stream] = stream_counts.get(stream,0)+1
                                state["buffer_high_water"] = max(state["buffer_high_water"],len(rows))
                                if exchange is None:
                                    continue
                                elapsed = (received - exchange).total_seconds() * 1000
                                delays.append(elapsed)
                                state["late"] += elapsed > 2000
                                previous = last_times.get((iid, stream), exchange)
                                state["out_of_order"] += exchange < previous
                                last_times[iid, stream] = max(previous, exchange)
                    if tick >= pulse:
                        healthy = topology <= acknowledged
                        if healthy and gap:
                            await asyncio.to_thread(finish_gap,gap)
                            gap = None
                        rows.extend(health_rows(assets, now(), connection, healthy))
                        sample = sorted(delays)
                        state.update(connected=healthy, error=None, assets=sorted(assets),
                                     subscriptions=len(acknowledged & topology), buffered=len(rows),
                                     latency_ms={f"p{p}": round(sample[(len(sample)-1)*p//100], 2)
                                                 for p in (50, 95, 99)} if sample else {})
                        pulse = tick + 1
                    if rows and (tick >= flush or len(rows) >= 250):
                        await asyncio.to_thread(persist, rows, state)
                        rows.clear()
                        flush = tick + .25
        except Exception as exc:
            state["unconfirmed_batch_rows"] += len(rows)
            if assets and gap is None:
                gap = assets,last_received,"storage_threshold" if str(exc)=="storage_threshold" else "websocket_disconnect"
            state.update(connected=False, error=str(exc)[:220], reconnects=state["reconnects"] + 1)
            log.warning("Research feed disconnected: %s", exc)
            try:
                await asyncio.to_thread(persist, list(health_rows(assets, now(), connection, False)), state)
            except Exception:
                log.exception("Unable to persist research outage")
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as lease:
        if not lease.execute("SELECT pg_try_advisory_lock(6022032) AS locked").fetchone()["locked"]:
            raise SystemExit("Research collector already running")
        lease.commit()
        asyncio.run(collect())

