from datetime import timedelta

from app.config import MINUTES


def interval_coverage(db, instrument_id, interval, start, end):
    step = timedelta(minutes=MINUTES[interval])
    # Count candle slots whose opening time is inside the requested half-open range.
    _, remainder = divmod(start.timestamp(), step.total_seconds())
    first = start if remainder == 0 else start + timedelta(seconds=step.total_seconds() - remainder)
    slots = max(0, int(-((first - end) // step)))
    existing = {row["open_ts"] for row in db.execute("""
        SELECT open_ts FROM candles WHERE instrument_id=%s AND interval=%s
        AND is_closed AND open_ts >= %s AND open_ts < %s
    """, (instrument_id, interval, first, end))}
    missing = 0
    gaps = []
    previous_missing = False
    for index in range(slots):
        stamp = first + index * step
        if stamp in existing:
            previous_missing = False
            continue
        missing += 1
        if previous_missing:
            if gaps and gaps[-1]["end"] == stamp:
                gaps[-1]["end"] = min(end, stamp + step)
        elif len(gaps) < 100:
            gaps.append(dict(start=stamp, end=min(end, stamp + step)))
        previous_missing = True
    return dict(instrument_id=instrument_id, interval=interval, start=start, end=end, expected=slots,
                available=slots - missing, missing=missing, coverage=(slots - missing) / slots if slots else None,
                gaps=gaps)

