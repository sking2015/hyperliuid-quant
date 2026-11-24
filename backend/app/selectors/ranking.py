from datetime import timedelta
from decimal import Decimal
from math import sqrt
from statistics import pstdev
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Selector = Literal["volume", "market_cap", "gainers", "losers", "volatility", "volume_spike"]


class Rules(BaseModel):
    selectors: list[Selector] = Field(default_factory=lambda: ["volume"], min_length=1, max_length=6)
    combine: Literal["UNION", "INTERSECTION"] = "UNION"
    top_n: int = Field(10, ge=1, le=200)
    exit_rank: int = Field(15, ge=1, le=500)
    capacity: int = Field(50, ge=1, le=300)
    refresh_seconds: int = Field(300, ge=30, le=86400)
    min_hold_seconds: int = Field(0, ge=0, le=604800)
    min_volume: Decimal = Field(Decimal(0), ge=0)
    max_spread_bps: Decimal | None = Field(None, ge=0)
    market_types: list[Literal["PERP", "SPOT", "HIP3", "OUTCOME"]] = Field(default_factory=lambda: ["PERP"])
    max_age_seconds: int = Field(300, ge=60, le=3600)

    @model_validator(mode="after")
    def validate_ranks(self):
        if self.exit_rank < self.top_n:
            raise ValueError("exit_rank must be at least top_n")
        return self


def metric(name, market, bars):
    if name == "volume":
        return market["volume_24h"] if market["quote"] == "USDC" else None
    if name in ("gainers", "losers"):
        change = market["change_24h"]
        return -change if name == "losers" and change is not None else change
    if name == "market_cap":
        return market["market_cap"]
    if len(bars) < 31 or any(b["open_ts"] - a["open_ts"] != timedelta(minutes=1)
                            for a, b in zip(bars, bars[1:])):
        return None
    if name == "volatility":
        prices = [float(bar["c"]) for bar in bars]
        if min(prices) <= 0:
            return None
        return Decimal(str(pstdev(b / a - 1 for a, b in zip(prices, prices[1:])) * sqrt(30)))
    if market["quote"] != "USDC":
        return None
    # OHLCV has base volume only. Close × volume is a quote turnover proxy.
    windows = [sum(bar["c"] * bar["v"] for bar in bars[start:start + 5]) for start in range(1, 31, 5)]
    baseline = sum(windows[:-1]) / 5
    return windows[-1] / baseline if baseline > 0 else None


def select(snapshot, history, config, as_of, blocked=frozenset()):
    cutoff = as_of - timedelta(seconds=config.max_age_seconds)
    eligible = []
    for market in snapshot:
        if (market["instrument_id"] in blocked or market["status"] != "ACTIVE"
                or market["market_type"] not in config.market_types
                or market["as_of"] is None or not cutoff <= market["as_of"] <= as_of
                or market["quality"] != "ok"):
            continue
        if config.min_volume and (market["quote"] != "USDC" or not market["volume_24h"]
                                  or market["volume_24h"] < config.min_volume):
            continue
        if config.max_spread_bps is not None:
            bid, ask, book_ts = market["bid"], market["ask"], market["book_ts"]
            if not bid or not ask or ask < bid or not book_ts or book_ts < cutoff:
                continue
            if (ask - bid) * 20000 / (ask + bid) > config.max_spread_bps:
                continue
        eligible.append(market)
    results = {}
    warnings = []
    selectors = list(dict.fromkeys(config.selectors))
    for name in selectors:
        scores = []
        for market in eligible:
            iid = market["instrument_id"]
            bars = [bar for bar in history.get(iid, ()) if bar["close_ts"] < as_of]
            if bars and bars[-1]["close_ts"] < as_of - timedelta(minutes=2):
                bars = []
            value = metric(name, market, bars)
            if value is not None:
                scores.append((value, iid, market["as_of"]))
        missing = len(eligible) - len(scores)
        if missing or not scores:
            warnings.append({"selector": name, "unavailable": missing, "eligible": len(eligible)})
        for rank, (value, iid, data_as_of) in enumerate(sorted(scores, key=lambda row: (-row[0], row[1])), 1):
            if rank > config.exit_rank:
                break
            result = results.setdefault(iid, dict(instrument_id=iid, rank=rank, score=value,
                                                  reason=name, data_quality="ok", data_as_of=data_as_of,
                                                  as_of=as_of, sources={}))
            result["sources"][name] = rank
            if rank < result["rank"]:
                result.update(rank=rank, score=value, reason=name)
    ordered = sorted((row for row in results.values()
                      if config.combine == "UNION" or len(row["sources"]) == len(selectors)),
                     key=lambda row: (row["rank"], row["instrument_id"]))
    return ordered, warnings

