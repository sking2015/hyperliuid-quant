from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from app.config import MINUTES


def now():
    return datetime.now(timezone.utc)


def catalog(db, kind, dex, rows, as_of):
    with db.cursor() as cursor:
        cursor.executemany("""
            INSERT INTO instruments (instrument_id,market_type,dex,asset_key,exchange_asset_id,
                symbol,base,quote,rules,status,paper_supported)
            VALUES (%(instrument_id)s,%(market_type)s,%(dex)s,%(asset_key)s,%(exchange_asset_id)s,
                %(symbol)s,%(base)s,%(quote)s,%(rules)s,%(status)s,%(paper_supported)s)
            ON CONFLICT (instrument_id) DO UPDATE SET symbol=excluded.symbol, base=excluded.base,
                quote=excluded.quote, rules=excluded.rules, status=excluded.status,
                paper_supported=excluded.paper_supported, asset_key=excluded.asset_key
        """, [dict(item, rules=Jsonb(item["rules"])) for item, _ in rows])
        cursor.executemany("""
            INSERT INTO market_snapshots (instrument_id,as_of,mark,mid,oracle,volume_24h,change_24h,
                open_interest,funding,source,quality,market_cap,cap_as_of,cap_source)
            VALUES (%(instrument_id)s,%(as_of)s,%(mark)s,%(mid)s,%(oracle)s,%(volume_24h)s,%(change_24h)s,
                %(open_interest)s,%(funding)s,'hyperliquid_rest',%(quality)s,%(market_cap)s,%(cap_as_of)s,%(cap_source)s)
            ON CONFLICT DO NOTHING
        """, [dict({"market_cap": None, "cap_as_of": None, "cap_source": None}, **snap,
                   instrument_id=item["instrument_id"], as_of=as_of) for item, snap in rows])
    db.execute("""
        UPDATE instruments SET status='DELISTED',paper_supported=false
        WHERE market_type=%s AND dex=%s AND NOT (instrument_id=ANY(%s))
    """, (kind, dex, [item["instrument_id"] for item, _ in rows]))


def save_candles(db, bars):
    with db.cursor() as cursor:
        cursor.executemany("""
            INSERT INTO candles VALUES (%(instrument_id)s,%(interval)s,%(open_ts)s,%(close_ts)s,
                %(o)s,%(h)s,%(l)s,%(c)s,%(v)s,%(trades)s,%(is_closed)s,%(source)s,%(received_ts)s)
            ON CONFLICT (instrument_id,interval,open_ts) DO UPDATE
            SET close_ts=excluded.close_ts,o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,
                v=excluded.v,trades=excluded.trades,is_closed=excluded.is_closed,
                source=excluded.source,received_ts=excluded.received_ts
            WHERE (NOT candles.is_closed AND excluded.is_closed)
               OR (candles.is_closed AND candles.source LIKE 'import:%%' AND excluded.source='rest')
               OR (excluded.source NOT LIKE 'import:%%' AND candles.is_closed=excluded.is_closed
                   AND candles.received_ts <= excluded.received_ts)
        """, bars)


def missing_intervals(start, end, timestamps, interval):
    step = timedelta(minutes=MINUTES[interval])
    cursor = start
    for stamp in sorted(set(timestamps)):
        if cursor <= stamp < end:
            if stamp > cursor:
                yield cursor, stamp
            cursor = stamp + step
    if cursor < end:
        yield cursor, end


def record_gaps(db, instrument_id, interval, start, end):
    previous = db.execute(
        """
        DELETE FROM data_gaps WHERE instrument_id=%s AND data_type=%s
        AND reason='missing_or_no_trades' AND start_ts <= %s AND end_ts >= %s
        RETURNING start_ts,end_ts
        """,
        (instrument_id, interval, end, start),
    ).fetchall()
    if previous:
        start = min(start, *(row["start_ts"] for row in previous))
        end = max(end, *(row["end_ts"] for row in previous))
    rows = db.execute("""
        SELECT open_ts FROM candles WHERE instrument_id=%s AND interval=%s
        AND is_closed AND open_ts >= %s AND open_ts < %s
    """, (instrument_id, interval, start, end))
    for left, right in missing_intervals(start, end, (row["open_ts"] for row in rows), interval):
        db.execute("""
            INSERT INTO data_gaps VALUES (%s,%s,%s,%s,'missing_or_no_trades',true)
            ON CONFLICT (instrument_id,data_type,start_ts) DO UPDATE SET end_ts=excluded.end_ts
        """, (instrument_id, interval, left, right))


MARKETS = """
SELECT i.*, s.as_of, s.mark, s.mid, s.oracle, s.volume_24h, s.change_24h,
       s.open_interest, s.funding, CASE WHEN s.cap_as_of >= %(as_of)s - interval '24 hours'
           AND s.cap_as_of <= %(as_of)s THEN s.market_cap END AS market_cap,
       s.cap_as_of, s.cap_source, s.source, s.quality,
       book.exchange_ts AS book_ts, (book.payload->'bbo'->0->>'px')::numeric AS bid,
       (book.payload->'bbo'->1->>'px')::numeric AS ask
FROM instruments i
LEFT JOIN LATERAL (
    SELECT * FROM market_snapshots WHERE instrument_id=i.instrument_id AND as_of<=%(as_of)s
    ORDER BY as_of DESC LIMIT 1
) s ON true
LEFT JOIN LATERAL (
    SELECT exchange_ts, payload FROM market_events WHERE instrument_id=i.instrument_id AND event_type='bbo'
    AND received_ts<=%(as_of)s AND exchange_ts<=%(as_of)s
    ORDER BY exchange_ts DESC LIMIT 1
) book ON true
"""

