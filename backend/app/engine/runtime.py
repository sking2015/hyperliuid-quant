from collections import defaultdict, deque
from datetime import datetime, timezone
from types import MappingProxyType

from app.domain.ports import PositionView


class LiveClock:
    def now(self):
        return datetime.now(timezone.utc)


class VirtualClock:
    def __init__(self, current):
        self.current = current

    def now(self):
        return self.current

    def advance(self, stamp):
        if stamp < self.current:
            raise ValueError("event clock cannot move backwards")
        self.current = stamp


class InstanceContext:
    def __init__(self, instance_id, clock, account):
        self.instance_id = instance_id
        self.clock = clock
        self.warming = True
        self._account = account
        self._history = defaultdict(lambda: deque(maxlen=512))

    def position(self, instrument_id):
        position = self._account.positions.get(instrument_id)
        return PositionView(position.side, position.quantity) if position else None

    def history(self, instrument_id, interval, limit):
        return tuple(self._history[instrument_id, interval])[-limit:] if limit > 0 else ()


class StrategyInstance:
    def __init__(self, strategy, context, execution):
        self.strategy = strategy
        self.context = context
        self.execution = execution
        self.status = "CREATED"

    def start(self):
        if self.status != "CREATED":
            raise RuntimeError("instance already started")
        self.strategy.on_init(self.context)
        self.status = "WARMING"

    async def dispatch(self, event, warming=False):
        if self.status not in ("WARMING", "RUNNING"):
            raise RuntimeError("instance is not active")
        self.context.warming = warming
        self.status = "WARMING" if warming else "RUNNING"
        if event.event_type == "candle_closed":
            self.context._history[event.instrument_id, event.payload["interval"]].append(event)
        try:
            intents = (self.strategy.on_order_update(event, self.context) if event.event_type == "order_update"
                       else self.strategy.on_event(event, self.context))
            if not warming:
                for intent in intents:
                    await self.execution.submit(intent)
        except Exception:
            self.status = "ERROR"
            raise

    def stop(self):
        if self.status != "ERROR":
            self.strategy.on_stop(self.context)
            self.status = "STOPPED"

    def checkpoint(self):
        return MappingProxyType({"status": self.status, "strategy": self.strategy.snapshot_state()})

