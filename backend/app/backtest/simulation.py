from dataclasses import dataclass, replace
from decimal import Decimal

from app.backtest.research import examine_signals
from app.config import MINUTES
from app.domain.ports import MarketEvent
from app.engine.runtime import InstanceContext, StrategyInstance, VirtualClock

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class Position:
    side: str
    quantity: Decimal
    entry: Decimal
    time: object
    margin: Decimal
    fee: Decimal
    stop: Decimal
    target: Decimal | None
    reason: str
    funding: Decimal = ZERO
    trailing_rate: Decimal | None = None
    extreme: Decimal | None = None

    @property
    def trailing_price(self):
        if self.trailing_rate is None or self.extreme is None:
            return None
        return self.extreme * (ONE - self.sign * self.trailing_rate)

    def protective_stop(self):
        trailing = self.trailing_price
        if trailing is not None and self.sign * (trailing - self.stop) > 0:
            return trailing, "trailing_stop"
        return self.stop, "stop_loss"

    @property
    def sign(self):
        return ONE if self.side == "LONG" else -ONE

    def pnl(self, price):
        return self.sign * self.quantity * (price - self.entry)


class Account:
    def __init__(self, initial):
        self.cash = initial
        self.positions = {}
        self.ledger = {}
        self.trades = []
        self.rejections = []
        self.fees = ZERO
        self.funding = ZERO
        self.realized = ZERO
        self.entries = {}

    def book(self, key, kind, amount, stamp, instrument=None):
        if key in self.ledger:
            return False
        self.ledger[key] = dict(key=key, kind=kind, amount=amount, time=stamp, instrument_id=instrument)
        self.cash += amount
        return True

    def equity(self, marks):
        return self.cash + sum((p.pnl(marks[iid]) for iid, p in self.positions.items()), ZERO)


class RiskEngine:
    def reject(self, account, marks, iid, notional, margin, fee):
        equity = account.equity(marks)
        reserved = sum((p.margin for p in account.positions.values()), ZERO)
        exposure = sum((p.quantity * marks[symbol] for symbol, p in account.positions.items()), ZERO)
        if iid in account.positions:
            return "position_exists"
        if len(account.positions) >= 5:
            return "position_limit"
        if equity <= 0 or margin + fee > account.cash - reserved:
            return "insufficient_available_cash"
        if notional > equity or exposure + notional > equity:
            return "exposure_limit"
        return None


class HistoricalExecution:
    def __init__(self, account, config, marks):
        self.account = account
        self.config = config
        self.marks = marks
        self.pending = {}
        self.seen = set()
        self.risk = RiskEngine()
        self.ambiguities = 0
        self.updates = []
        self.signals = []

    async def submit(self, intent):
        # Order intent is stored now. A market event must subsequently supply a price.
        if intent.intent_id not in self.seen:
            self.seen.add(intent.intent_id)
            self.pending[intent.instrument_id] = intent
            self.signals.append(dict(instrument_id=intent.instrument_id, time=intent.signal_ts,
                                     reference=self.marks.get(intent.instrument_id), action=intent.action,
                                     side=intent.side, reason=intent.reason))

    def slipped(self, price, buy):
        return price * (ONE + self.config.slippage if buy else ONE - self.config.slippage)

    def fee(self, price, quantity):
        return price * quantity * self.config.fee_rate

    def on_open(self, iid, stamp, price, warming):
        self.marks[iid] = price
        if warming:
            return
        position = self.account.positions.get(iid)
        if position:
            stop_price, stop_reason = position.protective_stop()
            stop = position.sign * (price - stop_price) <= 0
            target = position.target is not None and position.sign * (price - position.target) >= 0
            if stop or target:
                self.close(iid, price if stop else position.target, stamp,
                           stop_reason if stop else "take_profit", self.account.entries[iid] + ":bracket")
        intent = self.pending.get(iid)
        if intent is None or intent.signal_ts >= stamp:
            return
        del self.pending[iid]
        if intent.action == "CLOSE":
            if iid in self.account.positions:
                self.close(iid, price, stamp, intent.reason, intent.intent_id)
            return
        if intent.action != "OPEN" or intent.target_notional is None:
            return
        entry = self.slipped(price, intent.side == "LONG")
        notional = intent.target_notional
        quantity = notional / entry
        margin = notional / self.config.params.leverage
        fee = self.fee(entry, quantity)
        rejected = self.risk.reject(self.account, self.marks, iid, notional, margin, fee)
        if rejected:
            self.account.rejections.append(dict(time=stamp, instrument_id=iid, reason=rejected,
                                                signal_time=intent.signal_ts))
            self.updates.append((iid, "REJECTED", intent.intent_id))
            return
        if not self.account.book(intent.intent_id + ":fee", "fee", -fee, stamp, iid):
            return
        self.account.fees += fee
        sign = ONE if intent.side == "LONG" else -ONE
        target = entry * (ONE + sign * intent.take_profit) if intent.take_profit is not None else None
        self.account.positions[iid] = Position(intent.side, quantity, entry, stamp, margin, fee,
                                              entry * (ONE - sign * intent.stop_loss), target, intent.reason,
                                              trailing_rate=intent.trailing_stop, extreme=entry)
        self.account.entries[iid] = intent.intent_id
        self.updates.append((iid, "FILLED", intent.intent_id))

    def close(self, iid, price, stamp, reason, key):
        position = self.account.positions[iid]
        exit_price = self.slipped(price, position.side == "SHORT")
        pnl = position.pnl(exit_price)
        fee = self.fee(exit_price, position.quantity)
        if not self.account.book(key + ":pnl", "realized_pnl", pnl, stamp, iid):
            return
        self.account.book(key + ":fee", "fee", -fee, stamp, iid)
        self.account.fees += fee
        self.account.realized += pnl
        self.account.trades.append(dict(instrument_id=iid, side=position.side, quantity=position.quantity,
                                       entry_time=position.time, exit_time=stamp, entry=position.entry,
                                       exit=exit_price, stop=position.stop, target=position.target,
                                       trailing_stop=position.trailing_price,
                                       reason=reason, entry_reason=position.reason,
                                       gross_pnl=pnl, fees=position.fee + fee, funding=position.funding,
                                       net_pnl=pnl - position.fee - fee + position.funding))
        del self.account.positions[iid]
        del self.account.entries[iid]
        self.updates.append((iid, "FILLED", key))

    def bracket(self, iid, bar):
        position = self.account.positions.get(iid)
        if position is None:
            return
        high, low = bar["h"], bar["l"]
        stop_price, stop_reason = position.protective_stop()
        adverse, favorable = (low, high) if position.side == "LONG" else (high, low)
        stop = position.sign * (adverse - stop_price) <= 0
        target = position.target is not None and position.sign * (favorable - position.target) >= 0

        if stop and target:
            self.ambiguities += 1
        key = self.account.entries[iid] + ":bracket"
        if stop:
            self.close(iid, stop_price, bar["close_ts"], stop_reason, key)
        elif target:
            self.close(iid, position.target, bar["close_ts"], "take_profit", key)
        elif position.trailing_rate is not None and position.sign * (favorable - position.extreme) > 0:
            # A new intrabar extreme only becomes actionable after this candle closes.
            self.account.positions[iid] = replace(position, extreme=favorable)

    def apply_funding(self, row):
        iid, stamp = row["instrument_id"], row["exchange_ts"]
        position = self.account.positions.get(iid)
        if position is None or position.time >= stamp:
            return
        amount = -position.sign * position.quantity * self.marks[iid] * row["funding_rate"]
        if self.account.book(f"{iid}:{stamp.isoformat()}:funding", "funding", amount, stamp, iid):
            self.account.funding += amount
            self.account.positions[iid] = replace(position, funding=position.funding + amount)


class HistoricalEventSource:
    """L1 event stream keyed by time, event precedence and instrument identity."""

    def __init__(self, bars, funding):
        self.bars = bars
        self.funding = funding

    async def events(self, start, end):
        events = []
        for bar in self.bars:
            # Opening events carry only the opening price.
            events.append((bar["open_ts"], 0, bar["instrument_id"], {"o": bar["o"]}))
            events.append((bar["close_ts"], 1, bar["instrument_id"], bar))
        events.extend((row["exchange_ts"], 2, row["instrument_id"], row) for row in self.funding)
        for stamp, order, iid, payload in sorted(events, key=lambda item: item[:3]):
            if stamp < start or (end is not None and stamp >= end):
                continue
            yield MarketEvent(f"{iid}:{stamp.isoformat()}:{order}", iid,
                              ("candle_open", "candle_closed", "funding")[order],
                              stamp, stamp, payload.get("source", "historical"), payload)


async def simulate(run_id, definition, config, bars, funding, progress):
    clock = VirtualClock(min(bar["open_ts"] for bar in bars))
    account = Account(config.params.initial_balance)
    marks = {}
    execution = HistoricalExecution(account, config, marks)
    instance = StrategyInstance(definition.strategy(config.params), InstanceContext(str(run_id), clock, account),
                                execution)
    instance.start()
    curve = [dict(time=config.start, equity=account.cash)]
    peak = account.cash
    max_drawdown = ZERO
    latest_sample = None
    events = HistoricalEventSource(bars, funding)
    total = len(bars) * 2 + len(funding)
    processed = 0
    async for event in events.events(clock.now(), config.end):
        processed += 1
        if processed % 256 == 0:
            progress(min(98, 1 + processed * 97 // total))
        clock.advance(event.exchange_ts)
        bar, iid, stamp = event.payload, event.instrument_id, event.exchange_ts
        if latest_sample is not None and stamp != latest_sample["time"]:
            if curve[-1]["time"] == latest_sample["time"]:
                curve[-1] = latest_sample
            else:
                curve.append(latest_sample)
            latest_sample = None
        warming = stamp < config.start
        if event.event_type == "candle_open":
            execution.on_open(iid, stamp, bar["o"], warming)
        elif event.event_type == "funding":
            if not warming:
                execution.apply_funding(bar)
        else:
            if not warming:
                execution.bracket(iid, bar)
            marks[iid] = bar["c"]
        for update_iid, status, key in execution.updates:
            await instance.dispatch(MarketEvent(f"{key}:order_update", update_iid, "order_update", stamp,
                                                stamp, "backtest_l1", {"status": status}), warming)
        execution.updates.clear()
        await instance.dispatch(event, warming)
        if not warming and event.event_type in ("candle_closed", "funding"):
            equity = account.equity(marks)
            latest_sample = dict(time=stamp, equity=equity)
    if latest_sample is not None:
        if curve[-1]["time"] == latest_sample["time"]:
            curve[-1] = latest_sample
        else:
            curve.append(latest_sample)
    for sample in curve:
        peak = max(peak, sample["equity"])
        max_drawdown = max(max_drawdown, (peak - sample["equity"]) / peak)
    instance.stop()
    research = examine_signals(execution.signals, bars, MINUTES[config.params.timeframe])
    initial = config.params.initial_balance
    if abs(initial + sum((row["amount"] for row in account.ledger.values()), ZERO) - account.cash) > Decimal("1e-18"):
        raise RuntimeError("account cash does not reconcile")
    equity = account.equity(marks)
    wins = [t["net_pnl"] for t in account.trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in account.trades if t["net_pnl"] < 0]
    metrics = dict(initial_balance=initial, final_cash=account.cash, final_equity=equity,
                   net_pnl=equity - initial, return_rate=(equity - initial) / initial,
                   max_drawdown=max_drawdown, win_rate=Decimal(len(wins)) / len(account.trades) if account.trades else None,
                   profit_factor=sum(wins) / -sum(losses) if losses else None, trade_count=len(account.trades),
                   fees=account.fees, funding=account.funding, realized_pnl=account.realized,
                   unrealized_pnl=equity - account.cash, intrabar_conflicts=execution.ambiguities)
    open_positions = [dict(instrument_id=iid, side=p.side, quantity=p.quantity, entry=p.entry,
                           mark=marks[iid], unrealized=p.pnl(marks[iid]))
                      for iid, p in sorted(account.positions.items())]
    return dict(metrics=metrics, equity_curve=curve, trades=account.trades,
                open_positions=open_positions, rejections=account.rejections,
                unfilled_intents=len(execution.pending), ledger=list(account.ledger.values()),
                strategy_state=dict(instance.checkpoint()), signals=execution.signals, signal_research=research)

