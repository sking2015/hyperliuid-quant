from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Literal, Mapping, Protocol


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    instrument_id: str
    event_type: str
    exchange_ts: datetime
    received_ts: datetime
    source: str
    payload: Mapping[str, object]
    sequence: int | None = None
    schema_version: int = 1


class Clock(Protocol):
    def now(self) -> datetime: ...


class EventSource(Protocol):
    def events(self, start: datetime, end: datetime | None) -> AsyncIterator[MarketEvent]: ...


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    instance_id: str
    instrument_id: str
    action: Literal["OPEN", "CLOSE", "CANCEL", "MODIFY"]
    side: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT", "STOP", "TAKE_PROFIT"]
    quantity: Decimal
    signal_ts: datetime
    reason: str
    reduce_only: bool
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None


class ExecutionPort(Protocol):
    async def submit(self, intent: TradeIntent) -> None: ...


class LiveExecutionDisabled:
    async def submit(self, intent: TradeIntent) -> None:
        raise PermissionError("Live execution is disabled")


class StrategyContext(Protocol):
    @property
    def clock(self) -> Clock: ...
    def history(self, instrument_id: str, interval: str, limit: int) -> tuple[MarketEvent, ...]: ...


class Strategy(Protocol):
    def on_init(self, ctx: StrategyContext) -> None: ...
    def on_event(self, event: MarketEvent, ctx: StrategyContext) -> list[TradeIntent]: ...
    def on_order_update(self, event: MarketEvent, ctx: StrategyContext) -> list[TradeIntent]: ...
    def on_stop(self, ctx: StrategyContext) -> None: ...
    def snapshot_state(self) -> dict: ...
    def restore_state(self, state: dict) -> None: ...

