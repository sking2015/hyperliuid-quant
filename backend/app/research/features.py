from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from math import ceil, floor, isfinite, log
from statistics import median

from app.research.config import FEATURE_VERSION

WINDOWS = (1, 5, 15, 30, 60, 180)
ZERO = (0., 0., 0., 0, 0)


def ms(value):
    return int(value.timestamp() * 1000) if value is not None else None


@dataclass
class AssetState:
    quotes: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    prefixes: dict = field(default_factory=dict)
    seconds: dict = field(default_factory=dict)
    returns: deque = field(default_factory=deque)
    active_at: int | None = None
    connection: object = None
    quote: tuple | None = None
    book: tuple | None = None
    context: tuple | None = None
    oi: dict = field(default_factory=dict)
    dirty: int | None = None
    interrupted_at: int | None = None


class FeatureEngine:
    def __init__(self, config):
        self.config = config
        self.assets = defaultdict(AssetState)
        self.tick = None
        self.quality = defaultdict(int)

    def reset(self, scope):
        self.assets.pop(scope, None)

    def on_event(self, event):
        iid = event["instrument_id"]
        state = self.assets[iid]
        available = ms(event["available_ts"])
        if self.tick is None or available > self.tick:
            raise ValueError("Future event cannot enter feature state")
        exchange = ms(event.get("exchange_ts"))
        effective = available if exchange is None else exchange
        lag = ms(event["received_ts"]) - effective
        kind, payload = event["event_type"], event["payload"]
        if state.connection != event["connection_id"]:
            if state.connection is not None:
                state.interrupted_at = self.tick
            state.connection = event["connection_id"]
            state.quote = state.book = None
            state.active_at = None
        if kind == "research.health":
            state.active_at = available if payload["connected"] else None
            if state.active_at is None:
                state.interrupted_at = self.tick
                state.quote = state.book = None
            return
        if effective > available or (kind != "research.context" and lag > self.config.late_ms):
            self.quality["late_or_future"] += 1
            return
        if exchange is None:
            self.quality["exchange_time_missing"] += 1
        if kind in ("research.bbo", "research.l2"):
            field_name = "quote" if kind.endswith("bbo") else "book"
            before = getattr(state, field_name)
            if before and before[0] > effective:
                self.quality["out_of_order"] += 1
                return
            data = payload.get("bbo") if kind.endswith("bbo") else payload.get("levels")
            setattr(state, field_name, (effective, available, data, event.get("quality") or []))
        elif kind == "research.trade":
            price, size = float(payload["px"]), float(payload["sz"])
            if min(price, size) <= 0 or not isfinite(price * size):
                self.quality["invalid_trade"] += 1
                return
            bucket = ceil(effective / 1000)
            record = state.totals.setdefault(bucket, [0., 0., 0., 0])
            record[{"buy":0, "sell":1}.get(payload.get("aggressor"), 2)] += price * size
            record[3] += 1
            state.dirty = min(state.dirty, bucket) if state.dirty is not None else bucket
        elif kind == "research.context" and payload.get("open_interest") is not None:
            state.context = available, float(payload["open_interest"])

    def advance(self, tick):
        if self.tick is not None and tick != self.tick + 1000:
            raise ValueError("FeatureEngine requires contiguous one-second ticks")
        self.tick = tick

    def seal(self, iid):
        s = self.assets[iid]
        t = self.tick // 1000
        healthy = (s.active_at is not None and self.tick-s.active_at <= self.config.health_age_ms
                   and s.interrupted_at != self.tick)
        s.seconds[t] = healthy
        keep = max(self.config.warmup_s, self.config.baseline_s + 180, 3600) + 3
        oldest = t - keep
        for storage in (s.totals, s.prefixes, s.seconds, s.quotes, s.oi):
            storage.pop(oldest - 1, None)
        dirty = t if s.dirty is None else s.dirty
        for second in range(max(oldest, min(t, dirty)), t + 1):
            prev = s.prefixes.get(second - 1, ZERO)
            flows = s.totals.get(second, ZERO)
            s.prefixes[second] = tuple(prev[i] + flows[i] for i in range(4)) + (
                prev[4] + int(s.seconds.get(second, False)),
            )
        s.dirty = None
        quote = s.quote
        if healthy and quote and "invalid_book" not in quote[3] and self.tick - quote[0] <= self.config.bbo_age_ms:
            bid, ask = quote[2]
            b, a = Decimal(bid["px"]), Decimal(ask["px"])
            mid = float((b + a) / 2)
            s.quotes[t] = (mid, (float(a - b) / mid) * 10000, bid, ask)
        if healthy and s.context and self.tick - s.context[0] <= 120_000:
            s.oi[t] = s.context[1]

    def research_covered(self, iid):
        state = self.assets[iid]
        tick = self.tick // 1000
        if tick not in state.quotes or tick - 60 not in state.quotes:
            return False

        def healthy_seconds(end, width):
            first, last = state.prefixes.get(end - width), state.prefixes.get(end)
            return last[4] - first[4] if first is not None and last is not None else 0

        threshold = self.config.min_coverage
        if (healthy_seconds(tick, 60) != 60
                or healthy_seconds(tick, self.config.warmup_s) / self.config.warmup_s < threshold):
            return False
        bins = self.config.baseline_s // 30
        complete = sum(healthy_seconds(tick - 30 * i, 30) == 30 for i in range(1, bins + 1))
        return complete / bins >= threshold

    def snapshot(self, iid, as_of_ms):
        if as_of_ms != self.tick:
            raise ValueError("Snapshots are only valid at the current as-of tick")
        s = self.assets[iid]
        t = as_of_ms // 1000
        q = s.quotes.get(t)
        result = {}
        def add(name, value, count=1, coverage=1., flags=()):
            result[name] = dict(value=value, valid=value is not None, sample_count=count,
                                coverage=coverage, as_of_ms=as_of_ms, version=FEATURE_VERSION,
                                quality_flags=list(flags))
        def window(end, width):
            a, b = s.prefixes.get(end - width), s.prefixes.get(end)
            if a is None or b is None:
                return ZERO
            return tuple(b[i] - a[i] for i in range(5))
        def vol(v):
            return sum(v[:3])
        healthy = s.seconds.get(t, False)
        add("mid", q[0] if q else None, flags=() if q else ("stale_or_invalid_bbo",))
        add("spread_bps", q[1] if q else None)
        add("healthy", healthy)
        warm = window(t, self.config.warmup_s)[4] / self.config.warmup_s
        add("warmup", warm >= self.config.min_coverage, coverage=warm)
        for w in WINDOWS:
            b, a, u, n, good = window(t, w)
            full = good == w
            known = b + a
            coverage = good / w
            add(f"tfi_{w}", (b-a)/known if full and known > 0 and u/(known+u) <= self.config.max_unknown else None, n, coverage)
            add(f"cvd_{w}", b-a if full else None, n, coverage)
            add(f"trades_{w}", n if full else None, n, coverage)
            add(f"buy_usdc_{w}", b if full else None, n, coverage)
            add(f"sell_usdc_{w}", a if full else None, n, coverage)
            add(f"unknown_usdc_{w}", u if full else None, n, coverage)
            add(f"volume_{w}", known+u if full else None, n, coverage)
            old = s.quotes.get(t-w)
            add(f"return_{w}", log(q[0]/old[0]) if full and q and old else None, 2 if old and q else 0, coverage)
        for w in (15,30,60):
            bins = self.config.baseline_s // w
            configs = [window(t - w * i, w) for i in range(1, bins+1)]
            base = [vol(v) for v in configs if v[4] == w]
            reference = median(base) if base else 0
            current = result[f"volume_{w}"]["value"]
            add(f"rvol_{w}", current/reference if current is not None and reference > 0
                and len(base)/bins >= self.config.min_coverage else None, len(base), len(base)/bins)
            if w == 60:
                cvd = result["cvd_60"]["value"]
                ret = result["return_60"]["value"]
                divisor = cvd / reference if cvd is not None and reference > 0 else None
                add("impact_60", ret / divisor if ret is not None and divisor is not None
                    and abs(divisor) >= self.config.impact_floor and len(base)/bins >= self.config.min_coverage else None,
                    len(base), len(base)/bins)
                add("impact_denominator_60", divisor)
        diffs = []
        for i in range(12):
            b, a, u, _, good = window(t - 5*i, 5)
            if good == 5 and b+a > 0 and u/(b+a+u) <= self.config.max_unknown:
                diffs.append(b-a)
        for label, sign in (("long",1),("short",-1)):
            add(f"persistence_{label}", sum(sign*v > 0 for v in diffs)/12
                if len(diffs) >= self.config.min_trade_buckets else None, len(diffs), len(diffs)/12)
        book = s.book
        for width in (5,10,20,50,100):
            self.depth(add, width, q, book if healthy else None, as_of_ms)
        for w in (60,300,900):
            newest, old = s.oi.get(t), s.oi.get(t-w)
            add(f"oi_change_{w}", newest/old-1 if newest is not None and old else None)
        ret = result["return_15"]["value"]
        while s.returns and s.returns[0][0] <= t - 3615:
            s.returns.popleft()
        # Subsample nonoverlapping 15-second benchmark returns, excluding the active window.
        for duration in (1800,3600):
            values = [v for k,v in s.returns if t-duration-15 < k <= t-15]
            center = median(values) if values else 0
            sigma = 1.4826 * median(abs(v-center) for v in values) if values else None
            coverage = len(values) / (duration // 15)
            valid = coverage >= self.config.min_coverage
            add(f"robust_sigma_{duration}", sigma if valid else None, len(values), coverage)
            add(f"shock_z_{duration}", (ret-center)/max(sigma,self.config.epsilon)
                if valid and sigma is not None and ret is not None else None, len(values), coverage)
        if t % 15 == 0 and ret is not None and (not s.returns or s.returns[-1][0] != t):
            s.returns.append((t,ret))
        reference = self.config.reference_asset
        other = self.assets.get(reference) if reference and reference != iid else None
        beta = self.config.factor_beta
        for w in (5,15,30,60):
            value = result[f"return_{w}"]["value"]
            old_other, new_other = (other.quotes.get(t-w), other.quotes.get(t)) if other else (None,None)
            add(f"market_residual_{w}", value - self.config.factor_alpha - beta*log(new_other[0]/old_other[0])
                if beta is not None and value is not None and old_other and new_other
                and self.config.factor_available_ms < as_of_ms - w*1000 else None)
        spread_sample = [s.quotes[k][1] for k in range(t-60,t) if k in s.quotes]
        spread_sorted = sorted(spread_sample)
        add("spread_median_60", median(spread_sample) if spread_sample else None, len(spread_sample), len(spread_sample)/60)
        add("spread_p90_60", spread_sorted[floor((len(spread_sorted)-1)*.9)] if spread_sorted else None,
            len(spread_sample), len(spread_sample)/60)
        return result, q

    def depth(self, add, width, quote, book, stamp):
        values = (None, None)
        flags = ()
        count = 0
        if quote and book and "invalid_book" not in book[3] and stamp-book[0] <= self.config.l2_age_ms:
            bids, asks = book[2]
            mid = quote[0]
            low, high = mid*(1-width/10000), mid*(1+width/10000)
            count = len(bids)+len(asks)
            flags = ("truncated_depth",) if float(bids[-1]["px"]) > low or float(asks[-1]["px"]) < high else ()
            values = (sum(float(x["px"])*float(x["sz"]) for x in bids if low <= float(x["px"]) <= mid),
                      sum(float(x["px"])*float(x["sz"]) for x in asks if mid <= float(x["px"]) <= high))
        bid, ask = values
        add(f"bid_depth_{width}", bid, count, flags=flags)
        add(f"ask_depth_{width}", ask, count, flags=flags)
        add(f"obi_{width}", (bid-ask)/(bid+ask) if bid is not None and bid+ask > 0 and not flags else None,
            count, flags=flags)

