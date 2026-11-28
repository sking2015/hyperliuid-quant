from hashlib import sha256
from itertools import groupby
from math import ceil
from pathlib import Path

from app.research.config import ResearchParameters
from app.research.features import FeatureEngine, ms
from app.research.store import stamp


def windows(left, right, parameters):
    warmup = max(parameters["warmup_s"], parameters["baseline_s"] + 60)
    follow = max(parameters["horizons_s"])
    return left - warmup * 1000, right - 1000 + follow * 1000


def replay_candidates(db, assets, parameters):
    warmup, follow = windows(0, 1000, parameters)
    return db.execute("""
        WITH pulses AS (
            SELECT instrument_id,available_ts,connection_id,payload->>'connected'='true' AS healthy,
                   lead(available_ts) OVER timeline AS next_ts,
                   lead(connection_id) OVER timeline AS next_connection,
                   lead(payload->>'connected'='true') OVER timeline AS next_healthy
            FROM market_events
            WHERE ingest_seq IS NOT NULL AND instrument_id=ANY(%(assets)s)
              AND event_type='research.health'
            WINDOW timeline AS (PARTITION BY instrument_id ORDER BY available_ts,ingest_seq)
        ), by_asset AS (
            SELECT instrument_id,range_agg(tstzrange(available_ts,next_ts)) AS covered
            FROM pulses
            WHERE healthy AND next_healthy AND connection_id IS NOT DISTINCT FROM next_connection
              AND next_ts > available_ts AND next_ts-available_ts <= %(health_age)s * interval '1 millisecond'
            GROUP BY instrument_id
        ), shared AS (
            SELECT range_intersect_agg(covered) AS covered FROM by_asset HAVING count(*)=%(asset_count)s
        ), gaps AS (
            SELECT range_agg(tstzrange(start_ts,end_ts)) AS missing FROM data_gaps
            WHERE instrument_id=ANY(%(assets)s) AND start_ts<end_ts
              AND data_type IN ('research.health','research.bbo','research.trade')
        ), spans AS (
            SELECT unnest(shared.covered-coalesce(gaps.missing,'{}'::tstzmultirange)) AS span FROM shared,gaps
        ), aligned AS (
            SELECT ceil(extract(epoch FROM lower(span)))::bigint*1000 - %(warmup)s AS first_ms,
                   floor(extract(epoch FROM least(upper(span),statement_timestamp())))::bigint*1000
                       - %(follow)s + 1000 AS end_ms
            FROM spans
        )
        SELECT first_ms AS start_ms,end_ms FROM aligned
        WHERE first_ms<end_ms AND NOT EXISTS (
            SELECT 1 FROM unnest(%(assets)s::text[]) AS asset(id)
            WHERE NOT EXISTS (
                SELECT 1 FROM market_events
                WHERE instrument_id=asset.id AND event_type='research.bbo' AND ingest_seq IS NOT NULL
                  AND NOT (coalesce(quality,'[]') ? 'invalid_book')
                  AND available_ts>=to_timestamp((first_ms-%(bbo_age)s)/1000.0)
                  AND available_ts<to_timestamp(end_ms/1000.0)
                  AND coalesce(exchange_ts,available_ts)<=available_ts
                  AND received_ts-coalesce(exchange_ts,available_ts)<=%(late)s * interval '1 millisecond'
                  AND available_ts-coalesce(exchange_ts,available_ts)<=%(bbo_age)s * interval '1 millisecond'
            ) OR NOT EXISTS (
                SELECT 1 FROM market_events
                WHERE instrument_id=asset.id AND event_type='research.trade' AND ingest_seq IS NOT NULL
                  AND available_ts BETWEEN to_timestamp((first_ms+%(warmup)s)/1000.0)
                                       AND to_timestamp((end_ms+%(follow)s-1000)/1000.0)
            )
        )
        ORDER BY end_ms-first_ms DESC,end_ms DESC
    """, dict(assets=assets, asset_count=len(assets), health_age=parameters["health_age_ms"],
              warmup=warmup, follow=follow, bbo_age=parameters["bbo_age_ms"], late=parameters["late_ms"])).fetchall()


def covered_slice(db, assets, config, left, right, warmup):
    engine = FeatureEngine(config)
    totals = dict.fromkeys(assets, 0)
    shared = [0]
    with db.cursor(name="research_coverage") as cursor:
        cursor.execute("""
            SELECT event_id,instrument_id,event_type,exchange_ts,received_ts,available_ts,
                   ingest_seq,connection_id,quality,payload
            FROM market_events WHERE ingest_seq IS NOT NULL AND instrument_id=ANY(%s)
              AND available_ts>%s AND available_ts<%s
            ORDER BY available_ts,ingest_seq,event_id
        """, (assets, stamp(warmup-1000), stamp(right)))
        batches = iter(groupby(cursor, lambda row: ceil(row["available_ts"].timestamp())*1000))
        next_tick, rows = next(batches, (None, None))
        for tick in range(warmup, right, 1000):
            engine.advance(tick)
            while next_tick is not None and next_tick <= tick:
                for row in rows:
                    engine.on_event(row)
                next_tick, rows = next(batches, (None, None))
            for asset in assets:
                engine.seal(asset)
            if tick >= left:
                covered = [asset for asset in assets if engine.research_covered(asset)]
                for asset in covered:
                    totals[asset] += 1
                shared.append(shared[-1] + (len(covered) == len(assets)))
    seconds = len(shared)-1
    if all(total / seconds >= config.min_coverage for total in totals.values()):
        return left, right

    # Any slice with sufficient common valid ticks also passes each asset's check.
    # Prefix minima let us find the widest such slice without checking every pair.
    balances = [count - index * config.min_coverage for index, count in enumerate(shared)]
    minima = []
    for index, balance in enumerate(balances):
        if not minima or balance < balances[minima[-1]]:
            minima.append(index)
    best = (0, 0)
    for end in range(seconds, 0, -1):
        while minima and balances[minima[-1]] <= balances[end] + 1e-9:
            start = minima.pop()
            if end-start > best[1]-best[0] and (shared[end]-shared[start])/(end-start) >= config.min_coverage:
                best = start, end
    return left + best[0]*1000, left + best[1]*1000


def longest_replay_range(db, assets, parameters, max_seconds):
    config = ResearchParameters.model_validate(parameters)
    cap = max_seconds * 1000
    best = (0, 0)
    for candidate in replay_candidates(db, assets, parameters):
        first, right = candidate["start_ms"], candidate["end_ms"]
        if right-first < best[1]-best[0]:
            break
        while right-first >= best[1]-best[0] and right > first:
            left = max(first, right-cap)
            warmup, _ = windows(left, right, parameters)
            start, end = covered_slice(db, assets, config, left, right, warmup)
            if (end-start, end) > (best[1]-best[0], best[1]):
                best = start, end
            if left == first or best[1]-best[0] == cap:
                break
            right -= cap//2
    return {"start_ms": best[0], "end_ms": best[1]} if best[1] > best[0] else {"start_ms": None, "end_ms": None}


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
