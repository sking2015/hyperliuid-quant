import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.exchange.hyperliquid.public import PublicClient, candle, perp_rows, spot_rows, timestamp
from app.market_data.repository import missing_intervals
from app.market_data.stream import parse_message, update_subscriptions
from app.selectors.ranking import Rules, select
from app.watchlist.service import project


def test_market_identity_and_spot_join(raw):
    tokens, spot = spot_rows(*raw["spot"])
    native = perp_rows(*raw["perp"], tokens, "", 0)
    meta, context = raw["perp"]
    hip = perp_rows({**meta, "universe": [{**meta["universe"][0], "name": "xyz:BTC"}]}, context, tokens, "xyz", 1)
    assert [r[0][0]["instrument_id"] for r in (native, spot, hip)] == ["PERP::0", "SPOT::10000", "HIP3:xyz:110000"]
    assert spot[0][1]["mark"] == Decimal("0.07156")
    assert spot[0][1]["open_interest"] is None
    assert not hip[0][0]["paper_supported"]


def test_candle_gaps_and_closed(raw):
    stamp = timestamp(raw["candle"]["T"] + 10)
    bar = candle(raw["candle"], "PERP::0", stamp, "rest")
    assert bar["is_closed"]
    assert not candle(raw["candle"], "PERP::0", stamp, "ws")["is_closed"]
    start = bar["open_ts"]
    step = timedelta(minutes=1)
    assert list(missing_intervals(start, start + 4 * step, [start + 2 * step, start, start], "1m")) == [
        (start + step, start + 2 * step), (start + 3 * step, start + 4 * step)]


def test_stream_event_identity(raw):
    stamp = timestamp(raw["candle"]["T"] + 10)
    assets = {"BTC": "PERP::0"}
    assert len(parse_message(raw["ws_bbo"], assets, stamp)[1]) == 1
    trades = parse_message(raw["ws_trades"], assets, stamp)[1]
    assert trades[0][0] != trades[1][0]
    assert trades[0][0] == parse_message(raw["ws_trades"], assets, stamp + timedelta(seconds=1))[1][0][0]
    bars, _ = parse_message({"channel": "candle", "data": raw["candle"]}, assets, stamp)
    assert bars[0]["source"] == "ws"


def test_subscription_changes():
    class Socket:
        messages = []

        async def send(self, data):
            self.messages.append(data)

    socket = Socket()
    asyncio.run(update_subscriptions(socket, {"BTC": "BTC"}, {"BTC": "BTC", "ETH": "ETH"}))
    assert len(socket.messages) == 3
    assert all("ETH" in message and "BTC" not in message for message in socket.messages)
    socket.messages.clear()
    asyncio.run(update_subscriptions(socket, {}, {"BTC": "BTC"}))
    assert len(socket.messages) == 3


def test_weighted_retry(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 200, json={"BTC": "1"})

    async def no_sleep(seconds):
        assert seconds == 1

    monkeypatch.setattr("app.exchange.hyperliquid.public.asyncio.sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = PublicClient(http)
            assert await client.info({"type": "allMids"}, 2) == {"BTC": "1"}
            assert client.used == 4

    asyncio.run(run())


def test_rank_excludes_future_stale_and_missing():
    stamp = timestamp(1795491000000)
    base = dict(status="ACTIVE", market_type="PERP", quote="USDC", quality="ok",
                as_of=stamp, market_cap=None, volume_24h=Decimal(12))
    rows = [dict(base, instrument_id=i) for i in ("b", "a")]
    rows += [dict(base, instrument_id="future", as_of=stamp + timedelta(seconds=1)),
             dict(base, instrument_id="stale", as_of=stamp - timedelta(minutes=10))]
    results, warnings = select(rows, {}, Rules(selectors=["volume", "market_cap"]), stamp)
    assert [r["instrument_id"] for r in results] == ["a", "b"]
    assert warnings == [{"selector": "market_cap", "unavailable": 2, "eligible": 2}]


def test_exit_rank_buffer_protects_existing_member():
    as_of = timestamp(1795491000000)
    rows = [dict(instrument_id=i, status="ACTIVE") for i in ("a", "b", "c")]
    ranked = [dict(instrument_id=i, rank=r, sources={"volume": r}) for r, i in enumerate(("a", "b", "c"), 1)]
    previous = [dict(instrument_id="b", automatic=True, manual=False, pinned=False,
                     blocked=False, entered_at=as_of - timedelta(hours=2))]
    result = project({"type": "DYNAMIC"}, Rules(top_n=1, exit_rank=2), previous, ranked, rows, as_of)
    assert [r["instrument_id"] for r in result] == ["b"]


def test_refuse_live_execution():
    from app.domain.ports import LiveExecutionDisabled

    with pytest.raises(PermissionError):
        asyncio.run(LiveExecutionDisabled().submit(None))


def test_stream_reconnect_resubscribes_and_marks_gap(monkeypatch):
    from collections import Counter

    from app.market_data import stream

    attempts = []
    gap = []

    class Socket:
        def __init__(self):
            self.messages = []

        async def __aenter__(self):
            attempts.append(self)
            return self

        async def __aexit__(self, *_):
            return False

        async def send(self, data):
            self.messages.append(data)

        async def recv(self):
            if len(attempts) == 1:
                raise OSError("test connection lost")
            ready.set()
            await asyncio.Future()

    monkeypatch.setattr(stream, "websocket", lambda *a, **k: Socket())
    monkeypatch.setattr(stream, "subscription_refs", lambda: ({"BTC": "PERP::0"}, Counter(BTC=2)))
    monkeypatch.setattr(stream, "mark_disconnect", lambda assets, start: gap.append(assets))
    monkeypatch.setattr(stream.random, "random", lambda: 0)

    async def run():
        nonlocal ready
        ready = asyncio.Event()
        state = {}
        task = asyncio.create_task(stream.stream_loop(state))
        try:
            await asyncio.wait_for(ready.wait(), 4)
            assert gap == [{"BTC": "PERP::0"}]
            assert state["subscriptions"] == 3 and state["subscription_references"] == 2
            assert state["ws_reconnects"] == 1 and state["ws_connected"]
            assert attempts[0].messages[:3] == attempts[1].messages[:3]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    ready = None
    asyncio.run(run())

def test_actual_ws_sample_shapes():
    import json
    from pathlib import Path

    messages = json.loads((Path(__file__).parent / "fixtures" / "hyperliquid_ws_mainnet.json").read_text())
    received = timestamp(1795491950000)
    decoded = [parse_message(message, {"BTC": "PERP::0"}, received) for message in messages]
    assert [len(bars) + len(events) for bars, events in decoded] == [1, 1, 1]
    assert decoded[2][0][0]["trades"] == 129

def test_contiguous_selector_windows():
    from app.selectors.ranking import metric

    stamp = timestamp(1795491000000)
    bars = [dict(open_ts=stamp + timedelta(minutes=i), c=Decimal(100), v=Decimal(4 if i > 25 else 2))
            for i in range(31)]
    assert metric("volume_spike", {"quote": "USDC"}, bars) == Decimal(2)
    assert metric("volatility", {"quote": "USDC"}, bars) == Decimal(0)
    assert metric("volume_spike", {"quote": "USDC"}, bars[:15] + bars[16:]) is None
