from bisect import bisect_right
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

HORIZONS = (1, 5, 15, 60)


def examine_signals(signals, bars, interval_minutes):
    """Evaluate forward observations only after the strategy has stopped."""
    by_instrument = defaultdict(list)
    for bar in bars:
        by_instrument[bar["instrument_id"]].append(bar)
    times = {iid: [bar["close_ts"] for bar in series] for iid, series in by_instrument.items()}
    totals = {minutes: [] for minutes in HORIZONS}
    for signal in signals:
        if signal["action"] != "OPEN":
            continue
        iid, stamp, reference = signal["instrument_id"], signal["time"], signal["reference"]
        observed = {}
        for minutes in HORIZONS:
            if minutes % interval_minutes or reference is None:
                continue
            until = stamp + timedelta(minutes=minutes)
            lower = bisect_right(times[iid], stamp)
            upper = bisect_right(times[iid], until)
            if upper - lower != minutes // interval_minutes or times[iid][upper - 1] != until:
                continue
            window = by_instrument[iid][lower:upper]
            high, low = max(bar["h"] for bar in window), min(bar["l"] for bar in window)
            signed = Decimal(1) if signal["side"] == "LONG" else Decimal(-1)
            favorable, adverse = (high, low) if signed > 0 else (low, high)
            result = dict(return_rate=signed * (window[-1]["c"] / reference - 1),
                          mfe=max(Decimal(0), signed * (favorable / reference - 1)),
                          mae=max(Decimal(0), -signed * (adverse / reference - 1)))
            observed[str(minutes)] = result
            totals[minutes].append(result)
        signal["observations"] = observed
    return [{"minutes": horizon, "samples": len(items), **{
        key: sum(item[key] for item in items) / len(items) if items else None
        for key in ("return_rate", "mfe", "mae")
    }} for horizon, items in totals.items()]

