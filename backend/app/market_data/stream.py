import asyncio
import hashlib
import json
import logging
import random
from collections import Counter
from datetime import timedelta

from psycopg.types.json import Jsonb
from websockets.asyncio.client import connect as websocket

from app.exchange.hyperliquid.public import WS, candle, timestamp
from app.market_data.repository import now, save_candles
from app.storage.db import connect

log = logging.getLogger(__name__)
STREAM_TYPES = ("candle", "bbo", "trades")
MAX_ASSETS = 250


def subscription_refs():
    with connect() as db:
        rows = db.execute("""
            SELECT i.asset_key, i.instrument_id FROM watchlist_members m JOIN watchlists w ON w.id=m.watchlist_id
            JOIN instruments i USING (instrument_id)
            WHERE w.enabled AND w.deleted_at IS NULL AND i.status='ACTIVE' AND i.market_type<>'OUTCOME'
              AND NOT m.blocked AND (m.manual OR m.pinned OR m.automatic)
            ORDER BY m.pinned DESC, m.manual DESC, i.instrument_id
        """).fetchall()
    return {row["asset_key"]: row["instrument_id"] for row in rows}, Counter(row["asset_key"] for row in rows)


def parse_message(message, assets, received):
    channel, data = message["channel"], message.get("data")
    if channel == "candle" and data["s"] in assets:
        return [candle(data, assets[data["s"]], received, "ws")], []
    if channel not in ("trades", "bbo"):
        return [], []
    events = []
    for item in data if channel == "trades" else [data]:
        iid = assets.get(item["coin"])
        if iid is None:
            continue
        trade = item.get("tid")
        key = json.dumps([iid, channel, item["time"], trade] if trade is not None else
                         [iid, channel, item], sort_keys=True, separators=(",", ":"))
        events.append((hashlib.sha256(key.encode()).hexdigest(), iid, channel, timestamp(item["time"]), received,
                       "hyperliquid_ws", 1, Jsonb(item)))
    return [], events


def write_messages(messages, assets):
    bars, events = [], []
    for message, received in messages:
        candles, raw = parse_message(message, assets, received)
        bars.extend(candles)
        events.extend(raw)
    if bars or events:
        with connect() as db:
            save_candles(db, bars)
            with db.cursor() as cursor:
                cursor.executemany("INSERT INTO market_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", events)


def mark_disconnect(assets, start):
    with connect() as db:
        with db.cursor() as cursor:
            cursor.executemany("""
                INSERT INTO data_gaps VALUES (%s,%s,%s,%s,'websocket_disconnect',false)
                ON CONFLICT DO NOTHING
            """, [(iid, stream, start, now()) for iid in set(assets.values()) for stream in ("bbo", "trades")])
            cursor.executemany("""
                UPDATE history_jobs SET next_due=now() WHERE instrument_id=%s AND interval='1m'
            """, [(iid,) for iid in set(assets.values())])


async def update_subscriptions(socket, current, desired):
    for method, symbols in (("unsubscribe", current.keys() - desired.keys()),
                            ("subscribe", desired.keys() - current.keys())):
        for coin in sorted(symbols):
            for channel in STREAM_TYPES:
                sub = {"type": channel, "coin": coin}
                if channel == "candle":
                    sub["interval"] = "1m"
                await socket.send(json.dumps({"method": method, "subscription": sub}))
                await asyncio.sleep(.04)


async def stream_loop(state):
    delay = 1
    current = {}
    last_received = now()
    outage = None
    while True:
        if state.get("collection_paused"):
            await asyncio.sleep(2)
            continue
        try:
            async with websocket(WS, open_timeout=10, max_queue=32, ping_interval=None) as socket:
                current = {}
                if outage:
                    await asyncio.to_thread(mark_disconnect, *outage)
                    outage = None
                loop = asyncio.get_running_loop()
                reconcile = ping = 0
                last_received = now()
                batch = []
                while True:
                    if state.get("collection_paused"):
                        outage = (current, last_received) if current else None
                        state.update(ws_connected=False, subscriptions=0)
                        break
                    tick = loop.time()
                    if tick >= reconcile:
                        assets, counts = await asyncio.to_thread(subscription_refs)
                        desired = dict(list(assets.items())[:MAX_ASSETS])
                        await update_subscriptions(socket, current, desired)
                        current = desired
                        state.update(ws_connected=True, subscriptions=len(current) * len(STREAM_TYPES),
                                     subscription_references=sum(counts.values()), subscription_overflow=len(assets) - len(current),
                                     ws_error=None)
                        reconcile = loop.time() + 15
                    if tick >= ping:
                        await socket.send('{"method":"ping"}')
                        ping = tick + 25
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=.4)
                        last_received = now()
                        delay = 1
                        message = json.loads(raw)
                        channel = message.get("channel")
                        data = message.get("data")
                        sample = data[-1] if channel == "trades" and data else data
                        if channel in ("bbo", "trades") and isinstance(sample, dict) and "time" in sample:
                            latency = max(0, int(last_received.timestamp() * 1000) - sample["time"])
                            state["ws_latency_ms"] = round(state.get("ws_latency_ms", latency) * .9 + latency * .1)
                            state["ws_data_at"] = last_received.isoformat()
                        if channel == "error":
                            state["ws_error"] = str(message.get("data"))[:250]
                        batch.append((message, last_received))
                    except asyncio.TimeoutError:
                        if now() - last_received > timedelta(seconds=55):
                            raise TimeoutError("websocket heartbeat timed out")
                    if batch and (tick >= state.get("_flush", 0) or len(batch) >= 100):
                        await asyncio.to_thread(write_messages, batch, current)
                        batch.clear()
                        state["_flush"] = tick + .5
        except Exception as exc:
            if current and outage is None:
                outage = current, last_received
            state.update(ws_connected=False, subscriptions=0, ws_error=str(exc)[:250],
                         ws_reconnects=state.get("ws_reconnects", 0) + 1)
            log.warning("market stream disconnected: %s", exc)
            await asyncio.sleep(delay + random.random())
            delay = min(60, delay * 2)

