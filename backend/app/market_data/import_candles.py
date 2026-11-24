import argparse
import csv
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.config import MINUTES
from app.market_data.repository import now, record_gaps, save_candles
from app.storage.db import connect, migrate

COLUMNS = ("open_ts", "open", "high", "low", "close", "volume", "trades")


def parse_row(row, instrument_id, interval, step, source, received):
    start = datetime.fromisoformat(row["open_ts"])
    if start.utcoffset() is None:
        raise ValueError("open_ts requires an explicit timezone")
    start = start.astimezone(timezone.utc)
    end = start + step
    if start.microsecond or int(start.timestamp()) % int(step.total_seconds()) or end > received:
        raise ValueError("open_ts must be interval-aligned and the candle must be closed")
    o, h, low, c, v = [Decimal(row[key]) for key in COLUMNS[1:-1]]
    trades = int(row["trades"])
    if (not all(value.is_finite() for value in (o, h, low, c, v))
            or low <= 0 or low > min(o, c) or h < max(o, c) or v < 0 or trades < 0):
        raise ValueError("invalid OHLCV or trade count")
    return dict(instrument_id=instrument_id, interval=interval, open_ts=start,
                close_ts=end - timedelta(milliseconds=1), o=o, h=h, l=low, c=c, v=v,
                trades=trades, source=source, is_closed=True, received_ts=received)


def import_csv(path, instrument_id, interval, source):
    if path.suffix.lower() != ".csv":
        raise ValueError("expected a CSV file")
    if not source.strip() or len(source) > 100:
        raise ValueError("source must be 1–100 characters")
    source = "import:" + source.strip()
    step = timedelta(minutes=MINUTES[interval])
    received = now()
    with path.open("r", encoding="utf-8-sig", newline="") as stream, connect() as db:
        # Hash and ingest through the same handle, preventing path replacement between passes.
        digest = hashlib.file_digest(stream.buffer, "sha256").hexdigest()
        stream.seek(0)
        db.execute("SELECT pg_advisory_xact_lock(6022029)")
        if not db.execute("SELECT 1 FROM instruments WHERE instrument_id=%s", (instrument_id,)).fetchone():
            raise ValueError("unknown instrument")
        existing = db.execute("""
            SELECT rows FROM candle_imports WHERE instrument_id=%s AND interval=%s AND checksum=%s AND source=%s
        """, (instrument_id, interval, digest, source)).fetchone()
        if existing:
            return existing["rows"]
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError("expected columns: " + ",".join(COLUMNS))
        first = last = None
        batch = []
        for count, row in enumerate(reader, 1):
            try:
                if None in row:
                    raise ValueError("unexpected extra columns")
                bar = parse_row(row, instrument_id, interval, step, source, received)
                stamp = bar["open_ts"]
                if last is not None and stamp <= last:
                    raise ValueError("rows must be strictly increasing and unique")
            except (TypeError, ValueError, InvalidOperation, OverflowError) as exc:
                raise ValueError(f"line {count + 1}: {exc}") from exc
            first = first or stamp
            last = stamp
            batch.append(bar)
            if len(batch) == 1000:
                save_candles(db, batch)
                batch.clear()
        if first is None:
            raise ValueError("CSV has no candle rows")
        save_candles(db, batch)
        record_gaps(db, instrument_id, interval, first, last + step)
        db.execute("""
            INSERT INTO candle_imports VALUES (%s,%s,%s,%s,%s,%s,%s,now())
        """, (instrument_id, interval, digest, source, count, first, last + step))
        return count


def main():
    parser = argparse.ArgumentParser(description="Import explicitly selected local closed candle CSV")
    parser.add_argument("path", type=Path)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--interval", choices=MINUTES, required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    migrate()
    print(import_csv(args.path, args.instrument, args.interval, args.source))


if __name__ == "__main__":
    main()

