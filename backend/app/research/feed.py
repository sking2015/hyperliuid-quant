import hashlib
import json
from time import perf_counter
from decimal import Decimal, InvalidOperation

from psycopg.types.json import Jsonb

from app.exchange.hyperliquid.public import timestamp
from app.storage.db import connect

CH = {"bbo": "research.bbo", "trades": "research.trade", "l2Book": "research.l2"}
PUBLICATION_LOCK = 6022034


def valid_levels(levels):
    try:
        bids, asks = levels
        if not bids or not asks:
            return False
        parsed = [[(Decimal(level["px"]), Decimal(level["sz"])) for level in side] for side in levels]
        return (all(px.is_finite() and sz.is_finite() and px > 0 and sz > 0 for side in parsed for px, sz in side)
                and parsed[0][0][0] <= parsed[1][0][0]
                and all(a[0] > b[0] for a, b in zip(parsed[0], parsed[0][1:]))
                and all(a[0] < b[0] for a, b in zip(parsed[1], parsed[1][1:])))
    except (TypeError, KeyError, ValueError, InvalidOperation):
        return False


def normalize(message, assets, received, connection):
    channel = message.get("channel")
    if channel not in CH:
        return
    data = message["data"]
    for raw in data if channel == "trades" else [data]:
        iid = assets.get(raw["coin"])
        if iid is None:
            continue
        item = {key: value for key, value in raw.items() if key != "users"}
        ms = item.get("time")
        exchange = timestamp(ms) if ms is not None else None
        flags = []
        if exchange is None:
            flags.append("exchange_time_missing")
        if channel == "l2Book":
            item["snapshot_type"] = "finite_snapshot"
            item["depth_limit"] = [len(side) for side in item["levels"]]
            flags.append("finite_depth")
            if not valid_levels(item["levels"]):
                flags.append("invalid_book")
        if channel == "bbo" and not valid_levels([[level] for level in item["bbo"]]):
            flags.append("invalid_book")
        if channel == "trades":
            item["aggressor"] = {"B": "buy", "A": "sell"}.get(item.get("side"), "unknown")
        token = [iid, channel, ms, item["tid"]] if channel == "trades" and item.get("tid") is not None else [iid, channel, item]
        if ms is None:
            token.extend([str(connection), received.isoformat()])
        key = hashlib.sha256(json.dumps(token, sort_keys=True).encode()).hexdigest()
        yield key, iid, CH[channel], exchange, received, Jsonb(item), connection, Jsonb(flags)


def health_rows(assets, received, connection, connected):
    for iid in assets.values():
        key = hashlib.sha256(f"{connection}:{iid}:{received.isoformat()}:{connected}".encode()).hexdigest()
        yield (key, iid, "research.health", None, received, Jsonb({"connected": connected}), connection, Jsonb([]))


def persist(rows, state):
    started = perf_counter()
    with connect() as db:
        # Hold the session lock across both transactions. A crashed writer leaves
        # durable unpublished rows for the next writer; neither reader sees them.
        db.execute("SELECT pg_advisory_lock(%s)", (PUBLICATION_LOCK,))
        queued = db.execute("SELECT clock_timestamp() AS stamp").fetchone()["stamp"]
        if rows:
            with db.cursor() as c:
                c.executemany("""
                  INSERT INTO market_events(event_id,instrument_id,event_type,exchange_ts,received_ts,source,
                    schema_version,payload,connection_id,quality)
                  VALUES (%s,%s,%s,%s,%s,'hyperliquid_research_ws_v3',3,%s,%s,%s)
                  ON CONFLICT DO NOTHING
                """, rows)
                state["duplicates"] = state.get("duplicates", 0) + len(rows) - c.rowcount
        before_commit = perf_counter()
        db.commit()
        after_commit = perf_counter()
        published = publish_committed(db)
        state["stage_latency_ms"] = {
            "queue_max": round(max(0., max(((queued-r[4]).total_seconds()*1000 for r in rows), default=0.)), 2),
            "write": round((before_commit-started)*1000,2),
            "commit_ack": round((after_commit-before_commit)*1000,2),
            "publication": round((perf_counter()-after_commit)*1000,2),
        }
        state["published"] = state.get("published", 0) + published
        db.execute("""
            INSERT INTO worker_leases VALUES ('research_feed',now(),%s)
            ON CONFLICT(component) DO UPDATE SET heartbeat=excluded.heartbeat,state=excluded.state
        """, (Jsonb(state),))
        db.execute("""
            INSERT INTO research_stability_samples VALUES (date_trunc('minute',clock_timestamp()),%s)
            ON CONFLICT DO NOTHING
        """,(Jsonb(state),))


def publish_committed(db):
    # Sample the DB clock after the raw-data commit was acknowledged. The
    # published watermark and all rows behind it become readable atomically.
    stamp = db.execute("""
        INSERT INTO research_feed_publication(watermark) VALUES (clock_timestamp())
        ON CONFLICT(singleton) DO UPDATE SET watermark=GREATEST(
            research_feed_publication.watermark + interval '1 microsecond',clock_timestamp())
        RETURNING watermark
    """).fetchone()["watermark"]
    return db.execute("""
        UPDATE market_events m
        SET available_ts=GREATEST(%s,m.received_ts), ingest_seq=p.seq
        FROM (
            SELECT event_id,nextval('research_ingest_seq') AS seq FROM (
                SELECT event_id FROM market_events
                WHERE source='hyperliquid_research_ws_v3' AND ingest_seq IS NULL
                ORDER BY received_ts,event_id
            ) ordered
        ) p WHERE m.event_id=p.event_id
    """, (stamp,)).rowcount

