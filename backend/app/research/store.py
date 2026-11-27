from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from app.research.config import LABEL_VERSION


def stamp(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


def feed_rows(db, ids, after, until, input_sequence=None):
    rows = db.execute("""
        SELECT event_id,instrument_id,event_type,exchange_ts,received_ts,
               available_ts,ingest_seq,connection_id,quality,payload
        FROM market_events
        WHERE ingest_seq IS NOT NULL AND instrument_id=ANY(%s) AND available_ts>%s AND available_ts<=%s
          AND (%s::bigint IS NULL OR ingest_seq<=%s)
        ORDER BY available_ts,ingest_seq,event_id LIMIT 25001
    """, (list(ids), stamp(after), stamp(until), input_sequence,input_sequence)).fetchall()
    if len(rows)>25000:
        raise ValueError("Research replay shard exceeds 25000 events; reduce the number of assets")
    return rows


def persist_results(db, run_id, events, labels):
    with db.cursor() as cursor:
        cursor.executemany("""
            INSERT INTO research_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, [(e["id"],run_id,e["instrument_id"],e["variant"],e["detected_ms"],e["direction"],e["episode_id"],e["state"],Jsonb(e))
               for e in events])
        cursor.executemany("""
            INSERT INTO research_event_transitions VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, [(e["id"],sequence,state,e["detected_ms"],Jsonb(e["reasons"] if sequence else []))
               for e in events for sequence,state in enumerate(("CANDIDATE",e["state"]))])
        cursor.executemany("""
            INSERT INTO research_labels VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(event_id,horizon_s,version)
            DO UPDATE SET status=excluded.status, detail=excluded.detail WHERE research_labels.status='PENDING'
        """, [(key,h,LABEL_VERSION,value["status"],Jsonb(value)) for key,h,value in labels])


def finalize(db, run_id, status, error=None):
    reason = {"STOPPED":"task_stopped", "FAILED":"task_failed"}.get(status,"source_window_ended")
    db.execute("""
       UPDATE research_labels l SET status='CENSORED',detail=jsonb_build_object('status','CENSORED','reason',%s::text)
       FROM research_events e WHERE l.event_id=e.id AND e.run_id=%s AND l.status='PENDING'
    """, (reason,run_id,))
    db.execute("UPDATE research_runs SET status=%s,error=%s,updated_at=now() WHERE id=%s",
               (status,error,run_id))

