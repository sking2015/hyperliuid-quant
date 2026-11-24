import asyncio
import hashlib
import logging
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from app.backtest import simulation
from app.backtest.service import FrozenConfiguration, data_digest, encode, historical_inputs
from app.storage.db import connect, migrate
from app.strategies.registry import REGISTRY

log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


def claim():
    with connect() as db:
        return db.execute("""
            UPDATE backtest_runs SET status='RUNNING', started_at=now(), progress=1
            WHERE id=(SELECT id FROM backtest_runs WHERE status='QUEUED'
                      ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *
        """).fetchone()


def progress_for(run_id):
    def progress(percent):
        with connect() as db:
            result = db.execute("""
                UPDATE backtest_runs SET progress=%s WHERE id=%s AND NOT cancel_requested
                AND status='RUNNING' RETURNING id
            """, (percent, run_id)).fetchone()
            if result is None:
                raise Cancelled
    return progress


def execute(job):
    run_id = job["id"]
    cfg = FrozenConfiguration.model_validate(job["configuration"])
    definition = REGISTRY[cfg.strategy_id]
    if (definition.version, definition.code_hash) != (cfg.strategy_version, cfg.strategy_code_hash):
        raise ValueError("strategy code changed after submission")
    progress = progress_for(run_id)
    progress(1)
    with connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        bars, funding, manifest = historical_inputs(db, cfg, cfg.warmup)
    if not manifest["ready"]:
        raise ValueError("historical data coverage changed after preflight")
    manifest["sha256"] = data_digest(bars, funding)
    report = asyncio.run(simulation.simulate(run_id, definition, cfg, bars, funding, progress))
    report["manifest"] = manifest
    report["execution"] = dict(model="L1_NEXT_OPEN", bracket_conflict="stop_first",
                               funding_mark="last_known_price", open_positions="marked_to_market",
                               funding_coverage="partial" if manifest["funding_partial"] else "covered",
                               engine_sha256=hashlib.sha256(Path(simulation.__file__).read_bytes()).hexdigest())
    ledger = report.pop("ledger")
    with connect() as db:
        row = db.execute("SELECT cancel_requested FROM backtest_runs WHERE id=%s FOR UPDATE",
                         (run_id,)).fetchone()
        if row["cancel_requested"]:
            raise Cancelled
        with db.cursor() as cur:
            cur.executemany("INSERT INTO backtest_ledger VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            [(run_id, entry["key"], Jsonb(encode(entry))) for entry in ledger])
        db.execute("""
            UPDATE backtest_runs SET status='SUCCEEDED',progress=100,finished_at=now(),report=%s WHERE id=%s
        """, (Jsonb(encode(report)), run_id))


def run_once():
    job = claim()
    if not job:
        return False
    try:
        execute(job)
    except Cancelled:
        finish_failed(job["id"], "CANCELLED", None)
    except Exception as exc:
        log.exception("backtest run %s failed", job["id"])
        finish_failed(job["id"], "FAILED", str(exc)[:600])
    return True


def finish_failed(run_id, status, error):
    with connect() as db:
        db.execute("""
            UPDATE backtest_runs SET status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE %s END,
              error=CASE WHEN cancel_requested THEN NULL ELSE %s END,finished_at=now() WHERE id=%s
        """, (status, error, run_id))


def main():
    migrate()
    with connect() as lease:
        if not lease.execute("SELECT pg_try_advisory_lock(6022031) AS acquired").fetchone()["acquired"]:
            raise RuntimeError("backtest worker already running")
        lease.execute("""
            UPDATE backtest_runs SET status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'QUEUED' END,
            progress=0, finished_at=CASE WHEN cancel_requested THEN now() ELSE NULL END
            WHERE status='RUNNING'
        """)
        lease.commit()
        while True:
            lease.execute("SELECT 1")
            lease.commit()
            if not run_once():
                time.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

