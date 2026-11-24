import asyncio
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

import httpx

INFO = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"


def timestamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def number(value):
    return Decimal(value) if value is not None else None


class PublicClient:
    def __init__(self, http):
        self.http = http
        self.entries = deque()
        self.used = 0
        self.lock = asyncio.Lock()

    async def reserve(self, weight):
        async with self.lock:
            loop = asyncio.get_running_loop()
            while True:
                now = loop.time()
                while self.entries and self.entries[0][0] <= now - 60:
                    self.used -= self.entries.popleft()[1]
                if self.used + weight <= 950:
                    self.entries.append((now, weight))
                    self.used += weight
                    return
                await asyncio.sleep(max(.01, self.entries[0][0] + 60 - now))

    async def info(self, payload, weight=20):
        for attempt in range(3):
            await self.reserve(weight)
            response = await self.http.post(INFO, json=payload)
            if response.status_code == 429 and attempt < 2:
                retry = response.headers.get("Retry-After", "")
                await asyncio.sleep(min(60, max(2 ** attempt, int(retry) if retry.isdigit() else 0)))
                continue
            response.raise_for_status()
            return response.json()


def snapshot(ctx):
    price = number(ctx.get("markPx"))
    previous = number(ctx.get("prevDayPx"))
    return dict(mark=price, mid=number(ctx.get("midPx")), oracle=number(ctx.get("oraclePx")),
                volume_24h=number(ctx.get("dayNtlVlm")),
                change_24h=price / previous - 1 if price is not None and previous else None,
                open_interest=number(ctx.get("openInterest")), funding=number(ctx.get("funding")),
                quality="ok" if price is not None and price > 0 else "unavailable")


def asset(kind, dex, key, exchange_id, symbol, base, quote, size, leverage=None, delisted=False):
    return dict(instrument_id=f"{kind}:{dex}:{exchange_id}", market_type=kind, dex=dex,
                asset_key=key, exchange_asset_id=exchange_id, symbol=symbol, base=base, quote=quote,
                rules=dict(size_decimals=size, price_significant_figures=5 if size is not None else None,
                           price_max_decimals=(8 if kind == "SPOT" else 6) - size if size is not None else None,
                           max_leverage=leverage),
                status="DELISTED" if delisted else "ACTIVE",
                paper_supported=kind == "PERP" and quote == "USDC" and not delisted)


def spot_rows(meta, contexts):
    tokens = {token["index"]: token for token in meta["tokens"]}
    by_coin = {context["coin"]: context for context in contexts if context}
    rows = []
    for pair in meta["universe"]:
        base, quote = (tokens[index] for index in pair["tokens"])
        key = pair["name"]
        rows.append((asset("SPOT", "", key, 10000 + pair["index"],
                           f"{base['name']}/{quote['name']}", base["name"], quote["name"], base["szDecimals"]),
                     snapshot(by_coin.get(key, {}))))
    return tokens, rows


def perp_rows(meta, contexts, tokens, dex, dex_index):
    if len(meta["universe"]) != len(contexts):
        raise ValueError(f"perp metadata/context length mismatch for {dex}")
    collateral = tokens.get(meta.get("collateralToken", 0 if not dex else None), {}).get("name")
    return [(asset("HIP3" if dex else "PERP", dex, item["name"],
                   (100000 + dex_index * 10000 if dex else 0) + i,
                   item["name"], item["name"].split(":")[-1], collateral,
                   item["szDecimals"], item["maxLeverage"], item.get("isDelisted", False)),
             snapshot(ctx or {}))
            for i, (item, ctx) in enumerate(zip(meta["universe"], contexts))]


def outcome_rows(meta):
    return [(asset("OUTCOME", "", f"#{encoding}", 100000000 + encoding,
                   f"{outcome['name']} / {side['name']}", side["name"], outcome.get("quoteToken"), None),
             snapshot({}))
            for outcome in meta["outcomes"]
            for index, side in enumerate(outcome["sideSpecs"][:2])
            for encoding in [outcome["outcome"] * 10 + index]]


def candle(item, instrument_id, received, source):
    end = timestamp(item["T"])
    return dict(instrument_id=instrument_id, interval=item["i"], open_ts=timestamp(item["t"]),
                close_ts=end, **{key: number(item[key]) for key in "ohlcv"}, trades=item["n"],
                is_closed=source == "rest" and end < received, source=source, received_ts=received)

