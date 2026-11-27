from datetime import datetime, timedelta, timezone
from hashlib import file_digest
import json
from pathlib import Path
from uuid import UUID

from app.config import DATA_DIR
from app.research.store import stamp


class ArchivedFeed:
    """Read only immutable, pinned research Parquet partitions."""

    def __init__(self, db, run_id, root=DATA_DIR):
        root = Path(root).resolve()
        self.paths = []
        self.entries = []
        rows = db.execute("""
          SELECT m.path,m.checksum,m.start_ts,m.end_ts FROM market_data_manifest m
          JOIN research_data_pins p USING(path) WHERE p.run_id=%s ORDER BY m.path
        """,(run_id,))
        for row in rows:
            path = (root/row["path"]).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Pinned archive is outside DATA_DIR")
            with path.open("rb") as stream:
                if file_digest(stream,"sha256").hexdigest()!=row["checksum"]:
                    raise ValueError(f"Pinned archive checksum mismatch: {path.name}")
            self.paths.append(str(path))
            self.entries.append((str(path),row["start_ts"],row["end_ts"]))

    def read(self, ids, after, until):
        left,right = stamp(after),stamp(until)
        paths = [path for path,start,end in self.entries if start<=right and end>left]
        if not paths:
            return []
        import duckdb

        with duckdb.connect() as db:
            raw = db.execute("""
                SELECT event_id,instrument_id,event_type,epoch_us(exchange_ts),epoch_us(received_ts),
                       epoch_us(available_ts),ingest_seq,connection_id,quality,payload
                FROM read_parquet(?,hive_partitioning=false)
                WHERE available_ts>? AND available_ts<=? AND instrument_id IN (SELECT unnest(?))
                ORDER BY available_ts,ingest_seq,event_id LIMIT 25001
            """,(paths,left,right,list(ids))).fetchall()
        if len(raw)>25000:
            raise ValueError("Archived research replay shard exceeds 25000 events")
        epoch = datetime(1970,1,1,tzinfo=timezone.utc)
        def decode_time(value):
            return epoch+timedelta(microseconds=value) if value is not None else None
        return [dict(event_id=key,instrument_id=iid,event_type=kind,
                     exchange_ts=decode_time(exchange),received_ts=decode_time(received),
                     available_ts=decode_time(available),ingest_seq=seq,
                     connection_id=UUID(connection),quality=json.loads(quality),payload=json.loads(payload))
                for key,iid,kind,exchange,received,available,seq,connection,quality,payload in raw]

    def merge(self, hot, ids, after, until, input_sequence=None):
        rows = {row["event_id"]:row for row in self.read(ids,after,until)
                if input_sequence is None or row["ingest_seq"] <= input_sequence}
        rows.update((row["event_id"],row) for row in hot)
        if len(rows)>25000:
            raise ValueError("Combined research replay shard exceeds 25000 events")
        return sorted(rows.values(),key=lambda r:(r["available_ts"],r["ingest_seq"],r["event_id"]))

