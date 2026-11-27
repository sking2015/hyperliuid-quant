import argparse
import hashlib
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import quote

import duckdb

from app.config import DATA_DIR
from app.storage.db import connect, migrate

MARKET_TYPES = ("PERP", "HIP3", "SPOT", "OUTCOME")
CANDLE_SCHEMA = """
    instrument_id VARCHAR, interval VARCHAR, open_ts TIMESTAMPTZ, close_ts TIMESTAMPTZ,
    o VARCHAR, h VARCHAR, l VARCHAR, c VARCHAR, v VARCHAR, trades INTEGER,
    source VARCHAR, received_ts TIMESTAMPTZ
"""
CANDLE_QUERY = """
    SELECT c.instrument_id,c.interval,c.open_ts,c.close_ts,c.o::text AS o,c.h::text AS h,c.l::text AS l,c.c::text AS c,
           c.v::text AS v,c.trades,c.source,c.received_ts
    FROM candles c JOIN instruments i USING (instrument_id)
    WHERE c.is_closed AND i.market_type=%s AND c.open_ts>=%s AND c.open_ts<%s
    ORDER BY c.instrument_id,c.interval,c.open_ts
"""


EVENT_SCHEMA = """
    event_id VARCHAR, instrument_id VARCHAR, event_type VARCHAR, exchange_ts TIMESTAMPTZ,
    received_ts TIMESTAMPTZ, source VARCHAR, schema_version INTEGER, payload VARCHAR
"""
EVENT_QUERY = """
    SELECT e.event_id,e.instrument_id,e.event_type,e.exchange_ts,e.received_ts,
           e.source,e.schema_version,e.payload::text AS payload
    FROM market_events e JOIN instruments i USING (instrument_id)
    WHERE i.market_type=%s AND e.received_ts>=%s AND e.received_ts<%s AND e.event_type=%s
    ORDER BY e.received_ts,e.event_id
"""
RESEARCH_DATASETS = ("research.bbo","research.trade","research.l2","research.health","research.context")
RESEARCH_SCHEMA = EVENT_SCHEMA + ", available_ts TIMESTAMPTZ, ingest_seq BIGINT, connection_id VARCHAR, quality VARCHAR"
RESEARCH_QUERY = """
    SELECT event_id,instrument_id,event_type,exchange_ts,received_ts,source,schema_version,
           payload::text AS payload,available_ts,ingest_seq,connection_id::text AS connection_id,
           quality::text AS quality
    FROM market_events WHERE instrument_id=%s AND available_ts>=%s AND available_ts<%s
      AND event_type=%s AND ingest_seq IS NOT NULL ORDER BY available_ts,ingest_seq,event_id
"""
DATASETS = ("candles", "bbo", "trades", *RESEARCH_DATASETS)


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_day(day, root=DATA_DIR, dataset='candles', only_unpublished=False):
    if dataset not in DATASETS:
        raise ValueError('unsupported Parquet dataset')
    if day >= datetime.now(timezone.utc).date():
        raise ValueError("archive date must precede the current UTC date")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 256 * 1024**2:
        raise OSError("less than 256 MiB available for Parquet export")
    start = datetime.combine(day, time.min, timezone.utc)
    end = start + timedelta(days=1)
    partitions = []
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(6022028)")
        keys = MARKET_TYPES
        if dataset in RESEARCH_DATASETS:
            keys = [row["instrument_id"] for row in db.execute("""
                SELECT DISTINCT instrument_id FROM market_events e
                WHERE ingest_seq IS NOT NULL AND available_ts>=%s AND available_ts<%s AND event_type=%s
                AND (NOT %s OR NOT EXISTS (
                    SELECT FROM market_data_manifest m WHERE m.dataset_id=e.event_type
                    AND m.partition_date=%s AND m.market_type=e.instrument_id))
                ORDER BY instrument_id
            """, (start,end,dataset,only_unpublished,day))]
        for kind in keys:
            partition = archive_partition(db, root, day, kind, start, end, dataset)
            if partition:
                partitions.append(partition)
    return partitions


def archive_partition(db, root, day, kind, start, end, dataset):
    research = dataset in RESEARCH_DATASETS
    partition = f"asset={quote(kind,safe='')}" if research else f"market_type={kind}"
    folder = root / "parquet" / dataset / f"date={day}" / partition
    is_candle = dataset == "candles"
    schema = CANDLE_SCHEMA if is_candle else EVENT_SCHEMA
    query = CANDLE_QUERY if is_candle else EVENT_QUERY
    if research:
        schema, query = RESEARCH_SCHEMA, RESEARCH_QUERY
    parameters = (kind, start, end) if is_candle else (kind, start, end, dataset)
    with db.cursor(name="parquet_source") as rows, duckdb.connect() as out:
        rows.execute(query, parameters)
        batch = rows.fetchmany(2000)
        if not batch:
            return None
        out.execute("CREATE TABLE exported (" + schema + ")")
        placeholders = ", ".join("?" for _ in batch[0])
        count = 0
        while batch:
            out.executemany(f"INSERT INTO exported VALUES ({placeholders})",
                            [tuple(row.values()) for row in batch])
            count += len(batch)
            batch = rows.fetchmany(2000)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{uuid4().hex}.tmp"
        try:
            out.table("exported").write_parquet(str(temporary), compression="zstd")
            start_col, end_col = ("open_ts", "close_ts") if is_candle else ("received_ts", "received_ts")
            if research:
                start_col = end_col = "available_ts"
            observed_count, first, last = out.read_parquet(str(temporary)).aggregate(
                f"count(*), epoch_us(min({start_col})), epoch_us(max({end_col}))").fetchone()
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            first = epoch + timedelta(microseconds=first)
            last = epoch + timedelta(microseconds=last)
            if observed_count != count or not (start <= first < end and first <= last):
                raise ValueError("Parquet row count or timestamp validation failed")
            digest = checksum(temporary)
            destination = folder / f"{digest}.parquet"
            if destination.exists():
                if checksum(destination) != digest:
                    raise ValueError(f"checksum mismatch for {destination}")
            else:
                temporary.replace(destination)
            db.execute("""
                INSERT INTO market_data_manifest VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s,now())
                ON CONFLICT DO NOTHING
            """, (dataset, day, kind, destination.relative_to(root).as_posix(), digest, first, last, count))
            if research:
                db.execute("""
                    INSERT INTO research_data_pins(run_id,path)
                    SELECT r.id,%s FROM research_runs r JOIN research_configs c ON c.hash=r.config_hash
                    WHERE %s=ANY(r.asset_ids)
                      AND to_timestamp(coalesce(r.follow_end_ms,r.end_ms)/1000.0) >= %s
                      AND to_timestamp(r.start_ms/1000.0 - greatest((c.parameters->>'warmup_s')::integer,
                          (c.parameters->>'baseline_s')::integer+60)) <= %s
                    ON CONFLICT DO NOTHING
                """,(destination.relative_to(root).as_posix(),kind,first,last))
            return destination
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Export PostgreSQL market data into verifiable Parquet partitions")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--dataset", choices=DATASETS, default="candles")
    args = parser.parse_args()
    migrate()
    for path in archive_day(args.date, dataset=args.dataset):
        print(path)


if __name__ == "__main__":
    main()

