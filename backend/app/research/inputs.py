from hashlib import sha256
from pathlib import Path

from app.research.features import ms
from app.research.store import stamp


def windows(left, right, parameters):
    warmup = max(parameters["warmup_s"], parameters["baseline_s"] + 60)
    follow = max(parameters["horizons_s"])
    return left - warmup * 1000, right - 1000 + follow * 1000


def code_hash():
    digest = sha256()
    folder = Path(__file__).parent
    files = [*folder.glob("*.py"), folder.parent / "workers" / "research.py"]
    for path in sorted(files):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def replay_input(db, assets, warmup, left, right, follow, horizon_ms, bbo_age_ms):
    clock = db.execute("""
        SELECT clock_timestamp() AS current_time,
               (SELECT watermark FROM research_feed_publication) AS watermark
    """).fetchone()
    committed = ms(clock["watermark"] or clock["current_time"])
    limit = min(follow,committed)
    # A heartbeat immediately after the target proves the source spans it.
    # It may close the input boundary, but is not a feature/label input.
    health_probe_end = min(follow+5000,committed)
    rows = db.execute("""
        SELECT instrument_id,event_type,min(available_ts) AS first,max(available_ts) AS last,
               count(*) AS samples,
               count(*) FILTER (WHERE source<>'hyperliquid_research_ws_v3') AS legacy
        FROM market_events
        WHERE ingest_seq IS NOT NULL AND instrument_id=ANY(%s)
          AND available_ts BETWEEN %s AND %s
          AND (event_type='research.health' OR available_ts<=%s)
        GROUP BY instrument_id,event_type
    """, (assets, stamp(warmup), stamp(health_probe_end),stamp(limit))).fetchall()
    pins = db.execute("""
        SELECT path,market_type AS instrument_id,dataset_id AS event_type,start_ts AS first,end_ts AS last,
               row_count AS samples
        FROM market_data_manifest
        WHERE dataset_id LIKE 'research.%%' AND market_type=ANY(%s)
          AND start_ts<=%s AND end_ts>=%s ORDER BY path
    """, (assets, stamp(follow), stamp(warmup))).fetchall()
    gaps = db.execute("""
        SELECT instrument_id,data_type,start_ts,end_ts,reason FROM data_gaps
        WHERE instrument_id=ANY(%s) AND data_type LIKE 'research.%%'
          AND start_ts<%s AND end_ts>%s ORDER BY start_ts LIMIT 200
    """, (assets, stamp(follow), stamp(warmup))).fetchall()
    stale = db.execute("""
        WITH ordered AS (
            SELECT instrument_id,available_ts,
                   lead(available_ts) OVER(PARTITION BY instrument_id ORDER BY available_ts,ingest_seq) AS next_ts,
                   coalesce(exchange_ts,available_ts)+%s*interval '1 millisecond' AS expires
            FROM market_events
            WHERE ingest_seq IS NOT NULL AND instrument_id=ANY(%s) AND event_type='research.bbo'
              AND available_ts BETWEEN %s AND %s
              AND NOT (coalesce(quality,'[]') ? 'invalid_book')
        )
        SELECT instrument_id,greatest(available_ts,expires) AS start_ts,next_ts AS end_ts
        FROM ordered WHERE next_ts-greatest(available_ts,expires)>=interval '1 second'
        ORDER BY start_ts LIMIT 100
    """,(bbo_age_ms,assets,stamp(warmup),stamp(limit))).fetchall()
    bounds = {}
    for row in [*rows, *pins]:
        key = (row["instrument_id"], row["event_type"])
        first, last = ms(row["first"]), ms(row["last"])
        low, high = bounds.get(key,(first,last))
        bounds[key] = min(low,first),max(high,last)
    issues = []
    blocking = []
    tails = []
    for asset in assets:
        for stream in ("research.bbo", "research.trade", "research.health"):
            first,last = bounds.get((asset,stream),(None,None))
            if first is None:
                issues.append(dict(asset=asset,stream=stream,from_ms=warmup,to_ms=follow,reason="no_records"))
                blocking.append(asset)
                continue
            if first > warmup:
                issues.append(dict(asset=asset,stream=stream,from_ms=warmup,to_ms=first,reason="leading_gap"))
            if last < follow:
                issues.append(dict(asset=asset,stream=stream,from_ms=last,to_ms=follow,reason="trailing_gap"))
        health = bounds.get((asset,"research.health"))
        if health:
            tails.append(health[1]//1000*1000)
        bbo = bounds.get((asset,"research.bbo"))
        if bbo and (bbo[0]>=right or bbo[1]<left-bbo_age_ms):
            blocking.append(asset)
    # A symbol's missing quote is MISSING if the source timeline reaches its target.
    # A target past the frozen source boundary stays CENSORED.
    # The sequence cap includes already archived rows even after lawful hot removal.
    high_sequence = db.execute("SELECT last_value FROM research_ingest_seq").fetchone()["last_value"]
    close = min(limit//1000*1000, max(tails, default=right-1000))
    report = dict(warmup_start_ms=warmup, detection_start_ms=left,detection_end_ms=right,
                  follow_end_ms=follow,source_end_ms=close,max_horizon_ms=horizon_ms,
                  bbo_age_ms=bbo_age_ms,issues=issues,blocking=sorted(set(blocking)),
                  legacy_hot_rows=sum(row["legacy"] for row in rows),
                  recorded_gaps=[dict(asset=g["instrument_id"],stream=g["data_type"],
                                      from_ms=ms(g["start_ts"]),to_ms=ms(g["end_ts"]),reason=g["reason"])
                                 for g in gaps]+[
                      dict(asset=g["instrument_id"],stream="research.bbo",
                           from_ms=ms(g["start_ts"]),to_ms=ms(g["end_ts"]),reason="stale_hot_quote")
                      for g in stale])
    return high_sequence,close,report,[p["path"] for p in pins]
