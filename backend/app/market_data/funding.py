import asyncio
import logging
from datetime import timedelta

from app.exchange.hyperliquid.public import number, timestamp
from app.market_data.repository import now
from app.storage.db import connect

log = logging.getLogger(__name__)


def next_funding_job():
    with connect() as db:
        db.execute("""
            INSERT INTO funding_jobs(instrument_id)
            SELECT instrument_id FROM instruments WHERE market_type IN ('PERP','HIP3') AND status='ACTIVE'
            ON CONFLICT DO NOTHING
        """)
        return db.execute("""
            SELECT f.*,i.asset_key FROM funding_jobs f JOIN instruments i USING (instrument_id)
            WHERE i.status='ACTIVE' AND f.next_due <= now() ORDER BY f.next_due,f.instrument_id LIMIT 1
        """).fetchone()


def save_funding(job, items, start, end, received):
    rows = [(job["instrument_id"], timestamp(item["time"]), number(item["fundingRate"]),
             number(item.get("premium")), received, "hyperliquid_rest")
            for item in items if item["coin"] == job["asset_key"] and start <= item["time"] <= end]
    progress = max((row[1] for row in rows), default=timestamp(end))
    with connect() as db, db.cursor() as cursor:
        cursor.executemany("INSERT INTO funding_rates VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
        cursor.execute("""
            UPDATE funding_jobs SET next_due=now()+interval '1 hour', progress_ts=%s WHERE instrument_id=%s
        """, (progress, job["instrument_id"]))


def delay_job(job):
    with connect() as db:
        db.execute("UPDATE funding_jobs SET next_due=now()+interval '2 minutes' WHERE instrument_id=%s",
                   (job["instrument_id"],))


async def funding_loop(client, state):
    while True:
        job = None
        try:
            job = await asyncio.to_thread(next_funding_job)
            if job:
                received = now()
                end = int(received.timestamp() * 1000)
                earliest = received - timedelta(hours=24)
                start = max(earliest, job["progress_ts"] - timedelta(hours=1)) if job["progress_ts"] else earliest
                start_ms = int(start.timestamp() * 1000)
                rows = await client.info({"type": "fundingHistory", "coin": job["asset_key"],
                                          "startTime": start_ms, "endTime": end}, weight=23)
                await asyncio.to_thread(save_funding, job, rows, start_ms, end, received)
                state["funding_error"] = None
        except Exception as exc:
            state["funding_error"] = str(exc)[:250]
            log.exception("funding download failed")
            if job:
                try:
                    await asyncio.to_thread(delay_job, job)
                except Exception:
                    log.exception("funding job reschedule failed")
        await asyncio.sleep(20)

