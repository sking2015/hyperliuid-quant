import logging
import time
from datetime import datetime, timedelta, timezone

from app.market_data.archive import RESEARCH_DATASETS, archive_day
from app.storage.db import connect, migrate
from app.workers.depth import space_available


def archive_closed_days():
    today = datetime.now(timezone.utc).date()
    with connect() as db:
        days = db.execute("""
            SELECT DISTINCT (available_ts AT TIME ZONE 'UTC')::date AS day
            FROM market_events
            WHERE ingest_seq IS NOT NULL AND available_ts >= %s AND available_ts < %s
            ORDER BY day
        """,(today-timedelta(days=7),today)).fetchall()
    for row in days:
        day = row["day"]
        for dataset in RESEARCH_DATASETS:
            if not space_available():
                return
            archive_day(day,dataset=dataset,only_unpublished=True)


def main():
    logging.basicConfig(level=logging.INFO)
    migrate()
    last_day = None
    while True:
        today = datetime.now(timezone.utc).date()
        if last_day != today and space_available():
            try:
                archive_closed_days()
                last_day = today
            except Exception:
                logging.exception("Research archive cycle failed")
        time.sleep(300)


if __name__ == "__main__":
    main()
