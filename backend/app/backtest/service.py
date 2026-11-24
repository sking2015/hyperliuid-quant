import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from psycopg.types.json import Jsonb

from app.config import MINUTES
from app.storage.db import connect
from app.strategies.registry import EMAParameters, REGISTRY

MAX_BARS = 100_000


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def encode(value):
    return json.loads(json.dumps(value, default=json_value))


class BacktestSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    parameters: dict = Field(default_factory=dict)
    instrument_ids: list[str] = Field(default_factory=list, max_length=20)
    watchlist_id: str | None = None
    fee_rate: Decimal = Field(Decimal(".0005"), ge=0, le=Decimal(".05"))
    slippage: Decimal = Field(Decimal(".0005"), ge=0, le=Decimal(".05"))
    require_funding: bool = False

    @model_validator(mode="after")
    def valid_selection(self):
        if bool(self.watchlist_id) == bool(self.instrument_ids):
            raise ValueError("choose either instruments or one watchlist")
        return self


class BacktestDraft(BacktestSelection):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def valid_range(self):
        if (self.start.utcoffset() is None or self.end.utcoffset() is None
                or self.start >= self.end or self.end > datetime.now(timezone.utc)):
            raise ValueError("use an increasing historical range with explicit time zones")
        return self


def configure(db, draft):
    definition = REGISTRY.get(draft.strategy_id)
    if definition is None:
        raise HTTPException(422, "未知策略")
    try:
        params = definition.parameters.model_validate(draft.parameters)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    snapshot = None
    ids = sorted(set(draft.instrument_ids))
    if draft.watchlist_id:
        pool = db.execute("""
            SELECT id,name,version FROM watchlists WHERE id::text=%s AND deleted_at IS NULL FOR SHARE
        """, (draft.watchlist_id,)).fetchone()
        if not pool:
            raise HTTPException(404, "找不到观察池")
        ids = [row["instrument_id"] for row in db.execute("""
            SELECT instrument_id FROM watchlist_members WHERE watchlist_id=%s AND NOT blocked
            AND (manual OR pinned OR automatic) ORDER BY instrument_id
        """, (pool["id"],))]
        snapshot = dict(id=str(pool["id"]), name=pool["name"], version=pool["version"])
    if not 1 <= len(ids) <= 20:
        raise HTTPException(422, "回测需要 1 至 20 个交易对")
    markets = db.execute("""
        SELECT instrument_id,symbol,market_type,quote FROM instruments
        WHERE instrument_id=ANY(%s) ORDER BY instrument_id
    """, (ids,)).fetchall()
    if len(markets) != len(ids) or any(row["market_type"] != "PERP" or row["quote"] != "USDC" for row in markets):
        raise HTTPException(422, "M2 回测覆盖原生 USDC 永续")
    warmup = params.slow_ema + 1 if isinstance(params, EMAParameters) else 0
    return definition, draft.model_copy(update={"parameters": params.model_dump(mode="json"),
                                                 "instrument_ids": ids, "watchlist_id": None}), markets, snapshot, warmup


def historical_inputs(db, config, warmup):
    interval = config.params.timeframe
    step = timedelta(minutes=MINUTES[interval])
    warm_start = config.start - step * warmup
    bars = db.execute("""
        SELECT instrument_id, interval, open_ts, close_ts, o,h,l,c,v,trades,source
        FROM candles WHERE instrument_id=ANY(%s) AND interval=%s
          AND is_closed AND open_ts >= %s AND open_ts < %s AND close_ts < %s
        ORDER BY instrument_id,open_ts
    """, (config.instrument_ids, interval, warm_start, config.end, config.end)).fetchall()
    funding = db.execute("""
        SELECT instrument_id, exchange_ts, funding_rate, source FROM funding_rates
        WHERE instrument_id=ANY(%s) AND exchange_ts >= %s AND exchange_ts < %s ORDER BY instrument_id,exchange_ts
    """, (config.instrument_ids, config.start, config.end)).fetchall()
    expected = int((config.end - warm_start) // step)
    coverage = []
    for iid in config.instrument_ids:
        items = [bar for bar in bars if bar["instrument_id"] == iid]
        slots = {bar["open_ts"] for bar in items
                 if bar["close_ts"] == bar["open_ts"] + step - timedelta(milliseconds=1)
                 and (bar["open_ts"] - warm_start) % step == timedelta()
                 and 0 < bar["l"] <= min(bar["o"], bar["c"]) <= max(bar["o"], bar["c"]) <= bar["h"]}
        missing_slots = [warm_start + index * step for index in range(expected)
                         if warm_start + index * step not in slots]
        missing = len(missing_slots)
        invalid = len(items) - len(slots)
        missing_warmup = sum(stamp < config.start for stamp in missing_slots)
        start_hour = config.start.replace(minute=0, second=0, microsecond=0)
        if start_hour < config.start:
            start_hour += timedelta(hours=1)
        hour_count = max(0, int((config.end - start_hour - timedelta(microseconds=1)) // timedelta(hours=1)) + 1)
        hours = {row["exchange_ts"].replace(minute=0, second=0, microsecond=0)
                 for row in funding if row["instrument_id"] == iid and row["exchange_ts"] >= start_hour}
        missing_funding = max(0, hour_count - len(hours))
        coverage.append(dict(instrument_id=iid, expected=expected, available=expected - missing,
                             missing=missing, missing_funding_hours=missing_funding, invalid=invalid,
                             missing_warmup=missing_warmup, missing_requested=missing - missing_warmup,
                             first_missing=missing_slots[0] if missing_slots else None))
    ready = all(row["missing"] == 0 and row["invalid"] == 0 and (not config.require_funding or not row["missing_funding_hours"])
                for row in coverage)
    manifest = dict(coverage=coverage, warmup_bars=warmup, ready=ready,
                    funding_partial=any(row["missing_funding_hours"] for row in coverage),
                    candle_sources=sorted({b["source"] for b in bars}),
                    funding_sources=sorted({r["source"] for r in funding}))
    return bars, funding, manifest


class FrozenConfiguration(BacktestDraft):
    watchlist: dict | None = None
    markets: list[dict] = Field(default_factory=list)
    strategy_version: str
    strategy_code_hash: str
    warmup: int = 0

    @property
    def params(self):
        return REGISTRY[self.strategy_id].parameters.model_validate(self.parameters)


def freeze(db, draft):
    definition, normalized, markets, snapshot, warmup = configure(db, draft)
    normalized = normalized.model_copy(update={"start": normalized.start.astimezone(timezone.utc),
                                     "end": normalized.end.astimezone(timezone.utc)})
    step = MINUTES[normalized.parameters["timeframe"]] * 60
    if any(stamp.microsecond or int(stamp.timestamp()) % step for stamp in (normalized.start, normalized.end)):
        raise HTTPException(422, "起止时间必须对齐所选 K 线周期的 UTC 边界")
    if (int((normalized.end - normalized.start).total_seconds()) // step + warmup) * len(normalized.instrument_ids) > MAX_BARS:
        raise HTTPException(422, f"回测范围超过 {MAX_BARS} 根 K 线")
    return FrozenConfiguration(**normalized.model_dump(), markets=markets, watchlist=snapshot,
                               strategy_version=definition.version, strategy_code_hash=definition.code_hash, warmup=warmup)


def recent_slots(db, ids, interval, step):
    latest = db.execute("""
        SELECT i.instrument_id, c.open_ts
        FROM unnest(%s::text[]) AS i(instrument_id)
        CROSS JOIN LATERAL (
            SELECT open_ts FROM candles
            WHERE instrument_id=i.instrument_id AND interval=%s AND is_closed
              AND close_ts < now() AND open_ts <= now() - %s::interval
              AND close_ts = open_ts + %s::interval - interval '1 millisecond'
              AND l > 0 AND l <= LEAST(o,c) AND h >= GREATEST(o,c)
              AND mod(extract(epoch FROM open_ts), %s)=0
            ORDER BY open_ts DESC LIMIT 5000
        ) c
    """, (ids, interval, step, step, int(step.total_seconds()))).fetchall()
    slots = {iid: set() for iid in ids}
    for row in latest:
        slots[row["instrument_id"]].add(row["open_ts"])
    return slots


def continuous_range(common, step, warmup, max_slots, funded_hours=None):
    best = 0
    suggestion = None
    first = previous = candidate = None
    hour = timedelta(hours=1)
    for stamp in common:
        if previous is None or stamp != previous + step:
            first = stamp
            candidate = None
        previous = stamp
        if (stamp - first) // step < warmup:
            continue
        end = stamp + step
        if funded_hours is not None:
            settlement = stamp.replace(minute=0, second=0, microsecond=0)
            if settlement < stamp:
                settlement += hour
            while settlement < end and settlement in funded_hours:
                settlement += hour
            if settlement < end:
                candidate = None
                continue
        candidate = stamp if candidate is None else candidate
        start = max(candidate, end - (max_slots - warmup) * step)
        count = (end - start) // step
        if count >= best:
            best = count
            suggestion = dict(start=start, end=end)
    return suggestion


def explain_coverage(db, config, manifest):
    step = timedelta(minutes=MINUTES[config.params.timeframe])
    slots = recent_slots(db, config.instrument_ids, config.params.timeframe, step)
    symbols = {m["instrument_id"]: m["symbol"] for m in config.markets}
    for row in manifest["coverage"]:
        iid = row["instrument_id"]
        sample = slots[iid]
        row.update(symbol=symbols.get(iid, iid), local_start=min(sample) if sample else None,
                   local_end=max(sample) + step if sample else None)
    common = sorted(set.intersection(*slots.values()))
    suggestion = continuous_range(common, step, config.warmup, MAX_BARS // len(config.instrument_ids))
    return dict(manifest, interval=config.params.timeframe, require_funding=config.require_funding,
                requested_start=config.start, requested_end=config.end,
                suggested_range=suggestion)


def preflight(draft):
    with connect() as db:
        config = freeze(db, draft)
        _, _, manifest = historical_inputs(db, config, config.warmup)
        return manifest if manifest["ready"] else explain_coverage(db, config, manifest)


def queue(draft):
    with connect() as db:
        config = freeze(db, draft)
        _, _, manifest = historical_inputs(db, config, config.warmup)
        if not manifest["ready"]:
            raise HTTPException(422, {"message": "当前回测设置的数据覆盖不足",
                                      **explain_coverage(db, config, manifest)})
        return insert_run(db, config)


def insert_run(db, config):
    return db.execute("""
        INSERT INTO backtest_runs(id,configuration) VALUES (%s,%s)
        RETURNING id,status,progress,created_at
    """, (uuid4(), Jsonb(config.model_dump(mode="json")))).fetchone()


def queue_auto(selection):
    with connect() as db:
        definition, normalized, markets, snapshot, warmup = configure(db, selection)
        interval = normalized.parameters["timeframe"]
        step = timedelta(minutes=MINUTES[interval])
        ids = normalized.instrument_ids
        slots = recent_slots(db, ids, interval, step)
        common = sorted(set.intersection(*slots.values()))
        funded = None
        if common and normalized.require_funding:
            funded = {row["hour"] for row in db.execute("""
                SELECT date_trunc('hour', exchange_ts) AS hour FROM funding_rates
                WHERE instrument_id=ANY(%s) AND exchange_ts >= %s AND exchange_ts < %s
                GROUP BY 1 HAVING count(DISTINCT instrument_id)=%s
            """, (ids, common[0], common[-1] + step, len(ids)))}
        bounds = continuous_range(common, step, warmup, MAX_BARS // len(ids), funded)
        if not bounds:
            reason = ("找不到满足预热及完整资金费率要求的连续时段。可减少交易对、换 K 线周期，"
                      "或在接受资金费率缺失时关闭严格检查。" if normalized.require_funding else
                      "找不到满足预热的共同连续 K 线时段。请减少交易对、换 K 线周期或补充历史数据。")
            raise HTTPException(422, reason)
        config = FrozenConfiguration(**normalized.model_dump(), **bounds, markets=markets, watchlist=snapshot,
                                     strategy_version=definition.version, strategy_code_hash=definition.code_hash,
                                     warmup=warmup)
        _, _, manifest = historical_inputs(db, config, warmup)
        if not manifest["ready"]:
            raise HTTPException(422, {"message": "自动选择的时段未通过数据预检",
                                      **explain_coverage(db, config, manifest)})
        symbols = {market["instrument_id"]: market["symbol"] for market in markets}
        coverage = [
            {"instrument_id": row["instrument_id"], "symbol": symbols[row["instrument_id"]],
             "bars": row["available"] - warmup, "warmup_bars": warmup}
            for row in manifest["coverage"]
        ]
        return {**insert_run(db, config), **bounds, "interval": interval, "coverage": coverage}


def data_digest(bars, funding):
    return hashlib.sha256(json.dumps([bars, funding], default=json_value, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()

