from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from inspect import getsource
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ports import TradeIntent
from app.strategies.indicators import EMA


class StrategyParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeframe: Literal["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"] = "15m"
    exit_mode: Literal["FIXED", "TRAILING"] = "FIXED"
    trailing_stop: Decimal = Field(Decimal(".05"), gt=0, lt=1)
    take_profit: Decimal = Field(Decimal(".03"), gt=0, lt=1)
    stop_loss: Decimal = Field(Decimal(".015"), gt=0, lt=1)
    position_notional: Decimal = Field(Decimal(100), gt=0, le=1_000_000)
    initial_balance: Decimal = Field(Decimal(1000), gt=0, le=100_000_000)
    leverage: Decimal = Field(Decimal(1), ge=1, le=2)
    direction: Literal["BOTH", "LONG", "SHORT"] = "BOTH"


class EMAParameters(StrategyParameters):
    fast_ema: int = Field(9, ge=2, le=200)
    slow_ema: int = Field(21, ge=3, le=500)

    @model_validator(mode="after")
    def ordered(self):
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema must be less than slow_ema")
        return self


class SignalStrategy:
    def __init__(self, params):
        self.params = params

    def on_init(self, ctx):
        pass

    def on_stop(self, ctx):
        pass

    def on_order_update(self, event, ctx):
        return []

    def on_event(self, event, ctx):
        if event.event_type != "candle_closed" or event.payload["interval"] != self.params.timeframe:
            return []
        desired = self.signal(event)
        if desired is None or ctx.warming:
            return []
        position = ctx.position(event.instrument_id)
        side = position.side if position else desired
        if position and side == desired:
            return []
        action = "CLOSE" if position else "OPEN"
        if action == "OPEN" and self.params.direction not in ("BOTH", side):
            return []
        return [TradeIntent(f"{ctx.instance_id}:{event.event_id}:{action}", ctx.instance_id,
                            event.instrument_id, action, side, "MARKET",
                            position.quantity if position else Decimal(0), ctx.clock.now(),
                            "reverse_signal" if position else self.reason, bool(position),
                            target_notional=None if position else self.params.position_notional,
                            take_profit=self.params.take_profit if self.params.exit_mode == "FIXED" else None,
                            stop_loss=self.params.stop_loss,
                            trailing_stop=self.params.trailing_stop if self.params.exit_mode == "TRAILING" else None)]


class EMACrossoverStrategy(SignalStrategy):
    reason = "ema_crossover"

    def __init__(self, params):
        super().__init__(params)
        self.series = {}

    def signal(self, event):
        iid = event.instrument_id
        if iid not in self.series:
            self.series[iid] = (EMA(self.params.fast_ema), EMA(self.params.slow_ema), None)
        fast, slow, previous = self.series[iid]
        price = event.payload["c"]
        fast_value, slow_value = fast.update(price), slow.update(price)
        if fast_value is None or slow_value is None:
            return None
        difference = fast_value - slow_value
        self.series[iid] = fast, slow, difference
        if previous is not None:
            if previous <= 0 < difference:
                return "LONG"
            if previous >= 0 > difference:
                return "SHORT"
        return None

    def snapshot_state(self):
        return {iid: {"fast": vars(fast).copy(), "slow": vars(slow).copy(), "difference": difference}
                for iid, (fast, slow, difference) in self.series.items()}

    def restore_state(self, state):
        self.series.clear()
        for iid, values in state.items():
            fast, slow = EMA(self.params.fast_ema), EMA(self.params.slow_ema)
            for indicator, name in ((fast, "fast"), (slow, "slow")):
                for key in ("value", "total", "count"):
                    value = values[name][key]
                    setattr(indicator, key, Decimal(value) if value is not None and key != "count" else value)
            difference = values["difference"]
            self.series[iid] = fast, slow, Decimal(difference) if difference is not None else None


class DeterministicStrategy(SignalStrategy):
    reason = "deterministic_fixture"

    def __init__(self, params):
        super().__init__(params)
        self.counts = {}

    def signal(self, event):
        iid = event.instrument_id
        count = self.counts.get(iid, 0)
        self.counts[iid] = count + 1
        return "LONG" if count % 4 < 2 else "SHORT"

    def snapshot_state(self):
        return self.counts.copy()

    def restore_state(self, state):
        self.counts = dict(state)


@dataclass(frozen=True)
class Definition:
    id: str
    name: str
    version: str
    parameters: type[StrategyParameters]
    strategy: type[SignalStrategy]

    @property
    def code_hash(self):
        return sha256("".join(getsource(cls) for cls in (self.strategy, SignalStrategy, EMA)).encode()).hexdigest()

    def metadata(self):
        return dict(id=self.id, name=self.name, version=self.version, code_hash=self.code_hash,
                    parameter_schema=self.parameters.model_json_schema(),
                    defaults=self.parameters().model_dump(mode="json"), modes=["BACKTEST"])


REGISTRY = {
    entry.id: entry for entry in (
        Definition("ema_crossover", "EMA 交叉 · 基准策略", "1.0.0", EMAParameters, EMACrossoverStrategy),
        Definition("deterministic", "确定性开平仓 · 测试策略", "1.0.0", StrategyParameters, DeterministicStrategy),
    )
}

