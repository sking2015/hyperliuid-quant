import asyncio
import logging
from pathlib import Path

from app.config import DATA_DIR
from app.market_data.import_candles import import_csv
from app.storage.db import connect

log = logging.getLogger(__name__)


def input_path(filename):
    if not filename or Path(filename).name != filename or ":" in filename or "\\" in filename:
        raise ValueError("expected a filename under data/imports")
    root = (DATA_DIR / "imports").resolve()
    path = root / filename
    if path.suffix.lower() != ".csv" or path.resolve().parent != root or not path.is_file():
        raise ValueError("file must be a CSV under data/imports")
    return path


def run_one_job():
    with connect() as db:
        job = db.execute("""
            UPDATE data_import_jobs SET status='RUNNING',started_at=now() WHERE id=(
                SELECT id FROM data_import_jobs WHERE status IN ('QUEUED','RUNNING') ORDER BY created_at,id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            ) RETURNING *
        """).fetchone()
    if not job:
        return
    status = "DONE"
    count = error = None
    try:
        path = input_path(job["filename"])
        count = import_csv(path, job["instrument_id"], job["interval"], job["source"])
    except (ValueError, OSError) as exc:
        status, error = "ERROR", str(exc)[:400]
    except Exception:
        log.exception("data import %s failed", job["id"])
        status, error = "ERROR", "import failed; check worker logs"
    with connect() as db:
        db.execute("""
            UPDATE data_import_jobs SET status=%s,row_count=%s,error=%s,finished_at=now() WHERE id=%s
        """, (status, count, error, job["id"]))


async def import_loop():
    while True:
        try:
            await asyncio.to_thread(run_one_job)
        except Exception:
            log.exception("import job maintenance failed")
        await asyncio.sleep(5)

