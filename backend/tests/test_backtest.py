import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import UUID

import pytest

from app.backtest.service import FrozenConfiguration
from app.backtest.simulation import HistoricalEventSource, simulate
from app.strategies.registry import REGISTRY

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
IID = "PERP::0"


def rows(prices):
    bars = []
    for index, (o, h, low, close) in enumerate(prices):
        stamp = START + timedelta(minutes=index)
        bars.append(dict(instrument_id=IID, interval="1m", open_ts=stamp,
                         close_ts=stamp + timedelta(seconds=59, milliseconds=999),
                         o=D(o), h=D(h), l=D(low), c=D(close), source="fixture"))
    return bars


def config(count, **kwargs):
    strategy = REGISTRY["deterministic"]
    params = dict(timeframe="1m", take_profit=".03", stop_loss=".015")
    params.update(kwargs.pop("parameters", {}))
    return FrozenConfiguration(strategy_id=strategy.id, strategy_version=strategy.version,
                               strategy_code_hash=strategy.code_hash, parameters=params,
                               instrument_ids=[IID], start=START, end=START + timedelta(minutes=count),
                               fee_rate=kwargs.pop("fee_rate", 0), slippage=kwargs.pop("slippage", 0),
                               **kwargs)


def run(bars, cfg=None, funding=(), run_id=1):
    return asyncio.run(simulate(UUID(int=run_id), REGISTRY["deterministic"], cfg or config(len(bars)),
                                bars, funding, lambda _: None))


def test_next_open_stop_conflict_cost_and_funding():
    bars = rows([("100", "110", "90", "100"), ("100", "101", "99.9", "100"),
                 ("100", "104", "97", "101")])
    funding = [dict(exchange_ts=START + timedelta(minutes=1, seconds=30),
                    instrument_id=IID, funding_rate=D(".001"))]
    cfg = config(len(bars), slippage=".001", fee_rate=".001")
    result = run(bars, cfg, funding)
    [trade] = result["trades"]
    entry = D("100.1")
    qty = D(100) / entry
    exit_price = entry * D(".985") * D(".999")
    assert trade["entry_time"] == bars[1]["open_ts"] and trade["entry"] == entry
    assert trade["exit"] == exit_price and trade["reason"] == "stop_loss"
    assert trade["funding"] == -qty * D(100) * D(".001")
    assert trade["fees"] == D(".1") + exit_price * qty * D(".001")
    assert result["metrics"]["intrabar_conflicts"] == 1
    assert abs(result["metrics"]["final_cash"] - D(1000) - trade["net_pnl"]) < D("1e-20")
    assert result == run(bars, cfg, funding)
    isolated = run(bars, cfg, funding, run_id=2)
    assert result["metrics"] == isolated["metrics"]
    assert result["ledger"][0]["key"] != isolated["ledger"][0]["key"]


@pytest.mark.parametrize(("bars", "reason", "exit_price"), [
    ([("100","100","100","100"), ("100","104","100","103")], "take_profit", "103"),
    ([("100","100","100","100"), ("100","100","99","100"), ("97","98","96","97")], "stop_loss", "97"),
    ([("100","100","100","100"), ("100","101","99","100"), ("100","101","99","100"),
      ("102","102","102","102")], "reverse_signal", "102"),
])
def test_take_profit_gap_and_reverse(bars, reason, exit_price):
    trade = run(rows(bars))["trades"][0]
    assert trade["reason"] == reason and trade["exit"] == D(exit_price)
    assert trade["entry_time"] == START + timedelta(minutes=1)


def test_closed_events_no_future_and_last_bar_unfilled():
    bars = rows([("100","140","80","130")])

    async def read():
        return [event async for event in HistoricalEventSource(bars, []).events(START, None)]

    opening, closing = asyncio.run(read())
    assert set(opening.payload) == {"o"}
    assert opening.exchange_ts < closing.exchange_ts
    result = run(bars)
    assert result["unfilled_intents"] == 1
    assert result["open_positions"] == [] and result["trades"] == []


def test_independent_risk_rejects_without_ledger_mutation():
    bars = rows([("100","100","100","100"), ("100","101","99","100")])
    result = run(bars, config(len(bars), parameters={"initial_balance": 50}))
    assert result["rejections"][0]["reason"] == "insufficient_available_cash"
    assert not result["ledger"] and result["metrics"]["final_equity"] == D(50)


def test_disk_threshold_uses_host_probe_and_quota(monkeypatch):
    from types import SimpleNamespace
    from app.market_data import monitor

    monkeypatch.setattr(monitor, "HOST_PROBE", "host")
    monkeypatch.setattr(monitor, "MAX_DATA", 100)
    monkeypatch.setattr(monitor, "STOP_FREE", 10)
    monkeypatch.setattr(monitor, "WARN_FREE", 20)
    monkeypatch.setattr(monitor.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=8 if path == "host" else 500))
    assert monitor.disk_status(25, 25)["collection_paused"]
    monkeypatch.setattr(monitor, "HOST_PROBE", None)
    assert monitor.disk_status(57, 25)["disk_alert"] == "warning"
    assert monitor.disk_status(70, 25)["collection_paused"]


def test_positive_funding_credits_short_and_keeps_open_equity_separate():
    bars = rows([("100","100","100","100")] * 4)
    funding = [dict(exchange_ts=START + timedelta(minutes=3, seconds=30),
                    instrument_id=IID, funding_rate=D(".001"))]
    result = run(bars, config(len(bars), parameters={"direction": "SHORT"}), funding)
    assert result["metrics"]["funding"] == D(".1")
    assert result["metrics"]["final_equity"] == result["metrics"]["final_cash"] == D("1000.1")
    assert result["open_positions"][0]["side"] == "SHORT"
    assert result["trades"] == []


def test_future_changes_affect_research_but_not_first_strategy_intent():
    base = rows([("100","100","100","100"), ("100","104","100","103")])
    changed = rows([("100","100","100","100"), ("100","100","97","98")])
    first, second = run(base), run(changed)
    assert first["signals"][0]["time"] == second["signals"][0]["time"] == base[0]["close_ts"]
    assert first["signals"][0]["reference"] == second["signals"][0]["reference"] == D(100)
    assert first["signals"][0]["observations"]["1"]["return_rate"] == D(".03")
    assert second["signals"][0]["observations"]["1"]["return_rate"] == D("-.02")


def test_running_simulation_checks_cancellation():
    from app.workers.backtest import Cancelled

    bars = rows([("100","100","100","100")] * 150)
    seen = []

    def cancel(progress):
        seen.append(progress)
        raise Cancelled

    with pytest.raises(Cancelled):
        asyncio.run(simulate(UUID(int=1), REGISTRY["deterministic"], config(len(bars)), bars, [], cancel))
    assert len(seen) == 1 and 0 < seen[0] < 100

def test_auto_range_honors_warmup_common_gaps_and_funding():
    from app.backtest.service import continuous_range

    step = timedelta(minutes=15)
    slots = [START + step * i for i in (0, 1, 2, 3, 4, 5, 6, 7, 9)]
    # First hour has closed candles but no recorded settlement. Warmup can still use those candles.
    funded = {START + timedelta(hours=1)}
    result = continuous_range(slots, step, warmup=2, max_slots=20, funded_hours=funded)
    assert result == {"start": START + 2 * step, "end": START + 8 * step}
    assert continuous_range(slots, step, warmup=8, max_slots=20) is None

@pytest.mark.parametrize(("prices", "direction", "trigger", "fill", "reason"), [
    ([("100","100","100","100"), ("100","120","99","116"), ("116","117","113","115")],
     "LONG", "114", "114", "trailing_stop"),
    ([("100","100","100","100"), ("100","120","99","116"), ("110","115","109","111")],
     "LONG", "114", "110", "trailing_stop"),
    ([("100","100","100","100"), ("100","101","98","100")],
     "LONG", "95", "98.5", "stop_loss"),
    ([("100","100","100","100")] * 3 + [("100","101","80","82"), ("82","85","81","83")],
     "SHORT", "84", "84", "trailing_stop"),
])
def test_trailing_exit_keeps_fixed_stop_and_uses_only_prior_extremes(prices, direction, trigger, fill, reason):
    bars = rows(prices)
    result = run(bars, config(len(bars), parameters={"exit_mode": "TRAILING",
                                                    "trailing_stop": ".05", "direction": direction}))
    [trade] = result["trades"]
    assert trade["target"] is None
    assert trade["trailing_stop"] == D(trigger)
    assert trade["exit"] == D(fill)
    assert trade["reason"] == reason
    assert trade["exit_time"] >= bars[-1]["open_ts"]
