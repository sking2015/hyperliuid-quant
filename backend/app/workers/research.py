import hashlib
import logging
import time
from itertools import groupby
from math import ceil

from psycopg.types.json import Jsonb

from app.config import DATA_DIR
from app.research.ablation import ablation_report
from app.research.archive import ArchivedFeed
from app.research.detectors import DETECTORS
from app.research.engine import ResearchEngine
from app.research.features import ms
from app.research.inputs import code_hash, windows
from app.research.store import feed_rows, finalize, persist_results
from app.storage.db import connect, migrate

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, job, archive_root=DATA_DIR):
        self.job = job
        detector = DETECTORS[job.get("detector_id","S01")]
        self.config = detector.parameters.model_validate(job["parameters"])
        if self.config.fingerprint(detector.id,detector.version) != job["config_hash"]:
            raise ValueError("Research version differs from recorded configuration")
        if job.get("code_hash") and job["code_hash"] != code_hash():
            raise ValueError("Research implementation differs from recorded code hash")
        self.engine = ResearchEngine(job["id"],self.config,job["asset_ids"],job.get("detector_id","S01"))
        warmup,follow = windows(job["start_ms"],job["end_ms"],job["parameters"])
        self.cursor = warmup - 1000
        self.label_end = job.get("follow_end_ms") or follow
        if job.get("input_watermark_ms") is not None:
            self.label_end = min(self.label_end,job["input_watermark_ms"])
        self.sequence = job.get("input_sequence")
        self.digest = hashlib.sha256()
        with connect() as db:
            self.archive = ArchivedFeed(db,job["id"],archive_root)

    def advance(self, watermark, stopping=False):
        sample_started = time.perf_counter()
        job = self.job
        border = job["end_ms"]
        label_end = self.label_end
        if stopping:
            label_end = self.cursor
        latest = min(label_end,watermark,self.cursor + 10_000)
        if latest <= self.cursor and not stopping and self.cursor < label_end:
            return False
        with connect() as db:
            rows = feed_rows(db,self.engine.feature_assets,self.cursor,latest,self.sequence) if latest > self.cursor else []
            if self.archive.paths:
                rows = self.archive.merge(rows,self.engine.feature_assets,self.cursor,latest,self.sequence)
            # A record is first consumable at the whole-second tick at or after its availability.
            groups = iter(groupby(rows, lambda r: ceil(r["available_ts"].timestamp())*1000))
            key, group = next(groups,(None,None))
            events, labels = [], []
            feature_ms = 0.
            before_feature = self.engine.feature_ms
            before_detection = self.engine.detection_ms
            for tick in range(self.cursor + 1000,latest+1,1000):
                batch = []
                while key is not None and key <= tick:
                    batch.extend(group)
                    key, group = next(groups,(None,None))
                for row in batch:
                    self.digest.update(f"{row['event_id']}:{row['ingest_seq']}:{row['available_ts'].isoformat()}\n".encode())
                tick_started = time.perf_counter()
                e,l = self.engine.on_tick(tick,batch,job["start_ms"] <= tick < border)
                feature_ms += (time.perf_counter()-tick_started)*1000
                events.extend(e)
                labels.extend(l)
            self.cursor = latest
            persist_results(db,job["id"],events,labels)
            if job["mode"]=="LIVE":
                timing = {"engine_ms":round(feature_ms,2),
                          "feature_ms":round(self.engine.feature_ms-before_feature,2),
                          "detector_ms":round(self.engine.detection_ms-before_detection,2),
                          "batch_ms":round((time.perf_counter()-sample_started)*1000,2),
                          "schedule_lag_ms":max(0,int(time.time()*1000)-watermark),
                          "cursor_lag_ms":max(0,watermark-self.cursor),"assets":len(self.engine.assets)}
                db.execute("""
                    INSERT INTO worker_leases VALUES ('research_compute',clock_timestamp(),%s)
                    ON CONFLICT(component) DO UPDATE SET heartbeat=excluded.heartbeat,state=excluded.state
                """,(Jsonb(timing),))
            stats = dict(self.engine.stats, feature_quality=dict(self.engine.features.quality),
                         coverage=self.engine.asset_coverage)
            db.execute("""
                UPDATE research_runs SET status='RUNNING',cursor_ms=GREATEST(cursor_ms,%s),stats=%s,
                    source_hash=%s,updated_at=now() WHERE id=%s
            """, (self.cursor,Jsonb(stats),self.digest.hexdigest(),job["id"]))
            if self.cursor >= label_end or stopping:
                duration = (border-job["start_ms"])//1000
                covered = all(duration and value["covered"]/duration >= self.config.min_coverage
                              for value in self.engine.asset_coverage.values())
                status = "STOPPED" if stopping else "COMPLETE" if covered else "INSUFFICIENT_DATA"
                error = "One or more assets did not meet the required warmup, quote or baseline coverage." if status=="INSUFFICIENT_DATA" else None
                if status=="COMPLETE" and self.cursor < self.job["end_ms"]-1000+self.config.horizons_s[-1]*1000:
                    pending = db.execute("""
                        SELECT 1 FROM research_labels l JOIN research_events e ON e.id=l.event_id
                        WHERE e.run_id=%s AND l.status='PENDING' LIMIT 1
                    """,(job["id"],)).fetchone()
                    if pending:
                        status,error = "INSUFFICIENT_DATA","Frozen input does not cover one or more label targets."
                if status=="COMPLETE":
                    stats["ablation"] = ablation_report(db,job["id"],self.config)
                    db.execute("UPDATE research_runs SET stats=%s WHERE id=%s",(Jsonb(stats),job["id"]))
                finalize(db,job["id"],status,error)
                return True
        return False


def select_jobs():
    with connect() as db:
        publication = db.execute("SELECT watermark FROM research_feed_publication").fetchone()
        watermark = (ms(publication["watermark"])//1000)*1000 if publication else None
        jobs = db.execute("""
          SELECT r.*,c.parameters,c.detector_id FROM research_runs r JOIN research_configs c ON c.hash=r.config_hash
          WHERE r.status IN ('QUEUED','RUNNING') ORDER BY r.created_at,r.id LIMIT 4
        """).fetchall()
        return jobs,watermark


def run():
    resident = {}
    while True:
        jobs,watermark = select_jobs()
        for job in jobs:
            key = job["id"]
            try:
                if job["stop_requested"]:
                    with connect() as db:
                        finalize(db,key,"STOPPED")
                    resident.pop(key,None)
                    continue
                if key not in resident:
                    resident[key] = Runner(job)
                cutoff = resident[key].label_end if job["mode"]=="REPLAY" else watermark
                if cutoff is not None and resident[key].advance(cutoff):
                    resident.pop(key,None)
            except Exception as exc:
                log.exception("Research run %s failed",key)
                with connect() as db:
                    finalize(db,key,"FAILED",str(exc)[:500])
                resident.pop(key,None)
        time.sleep(.15 if any(job["mode"]=="REPLAY" for job in jobs) else 1)


def main():
    migrate()
    with connect() as lease:
        if not lease.execute("SELECT pg_try_advisory_lock(6022033) AS locked").fetchone()["locked"]:
            raise SystemExit("Research worker already running")
        lease.commit()
        run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

