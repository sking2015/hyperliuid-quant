from decimal import Decimal
from math import log


class ForwardReturnAnalyzer:
    def __init__(self, config):
        self.config = config

    def label(self, origin, end, ticks, direction, horizon):
        if end is None:
            return {"status":"MISSING", "reason":"missing_or_stale_target_bbo", "coverage":len(ticks)/horizon}
        sign = 1 if direction == "LONG" else -1
        path = [(elapsed, sign*log(quote[0]/origin[0])) for elapsed, quote in ticks]
        coverage = len(path)/horizon
        enough = coverage >= self.config.min_coverage
        best = max(path, key=lambda item:item[1], default=(0,0))
        worst = min(path, key=lambda item:item[1], default=(0,0))
        return dict(status="COMPLETE", coverage=coverage, grid_ms=1000,
                    return_log=sign*log(end[0]/origin[0]),
                    mfe=max(0,best[1]) if enough else None,
                    mae=min(0,worst[1]) if enough else None,
                    mfe_first_s=(best[0] if best[1]>0 else 0) if enough else None,
                    mae_first_s=(worst[0] if worst[1]<0 else 0) if enough else None,
                    execution=self.execution(origin,end,sign))

    def execution(self, origin, end, sign):
        open_level, exit_level = (origin[3],end[2]) if sign == 1 else (origin[2],end[3])
        entry = Decimal(open_level["px"])
        exit_price = Decimal(exit_level["px"])
        raw_entry,raw_exit = entry,exit_price
        quantity = Decimal(str(self.config.notional_usdc)) / entry
        if min(Decimal(open_level["sz"]),Decimal(exit_level["sz"])) < quantity:
            return dict(status="insufficient_depth", model="BBO_TAKER_FIXED_HORIZON")
        bps = Decimal(10000)
        slip = Decimal(str(self.config.slippage_bps))/bps
        fee = Decimal(str(self.config.taker_fee_bps))/bps
        entry *= 1+sign*slip
        exit_price *= 1-sign*slip
        cost = fee*(entry+exit_price)*quantity
        net = sign*quantity*(exit_price-entry)-cost
        origin_mid,end_mid = Decimal(str(origin[0])),Decimal(str(end[0]))
        gross = sign*quantity*(end_mid-origin_mid)
        spread = sign*quantity*((raw_entry-origin_mid)+(end_mid-raw_exit))
        slippage = sign*quantity*((entry-raw_entry)+(raw_exit-exit_price))
        return dict(status="filled", model="BBO_TAKER_FIXED_HORIZON", funding_included=False,
                    quantity=str(quantity), entry=str(entry), exit=str(exit_price),
                    fees_usdc=str(cost), spread_usdc=str(spread), slippage_usdc=str(slippage),
                    gross_usdc=str(gross), gross_return=float(gross/(entry*quantity)),
                    net_usdc=str(net), net_return=float(net/(entry*quantity)))

