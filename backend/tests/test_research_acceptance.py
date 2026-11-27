import math

import pytest

from app.research.config import ResearchParameters
from app.research.detectors import S01
from app.research.features import FeatureEngine
from app.research.labels import ForwardReturnAnalyzer
from test_research import BASE, IID, quote, row


def check(name, actual, expected):
    numeric = isinstance(expected, (float, int))
    error = abs(actual - expected) if numeric and isinstance(actual, (float, int)) else None
    print(f"{name}: expected={expected!r}; actual={actual!r}; error={error!r}")
    assert actual == (pytest.approx(expected, abs=1e-10, rel=1e-10) if error is not None else expected)


def test_manual_feature_fixture():
    config = ResearchParameters(warmup_s=60, baseline_s=60)
    engine = FeatureEngine(config)
    for t in range(1816):
        engine.advance(BASE + t * 1000)
        engine.on_event(row("health", {"connected": True}, t))
        # Log-return history alternates between two known levels with equal frequency.
        mid = 100 * math.exp((t // 15 // 2) * .001 + (.002 if t == 1815 else 0))
        engine.on_event(row("bbo", {"bbo": list(quote(mid)[2:])}, t))
        if 1756 <= t <= 1815 and (t - 1756) % 5 == 0:
            side = "sell" if t < 1771 else "buy"
            engine.on_event(row("trade", {"aggressor": side, "px": "1", "sz": "100"}, t))
        if t == 1815:
            engine.on_event(row("l2", {"levels": [
                [{"px": str(mid*.9999), "sz": "2"}, {"px": str(mid*.99), "sz": "1"}],
                [{"px": str(mid*1.0001), "sz": "1"}, {"px": str(mid*1.01), "sz": "1"}],
            ]}, t))
        engine.seal(IID)
        results, _ = engine.snapshot(IID, BASE + t * 1000)
        if t in (59, 60, 1815):
            check(f"warmup-{t}", results["warmup"]["value"], int(t >= 60))
    for key, expected in {
        "mid": mid, "spread_bps": .02 / mid * 10000,
        "bid_depth_5": mid*.9999*2, "ask_depth_5": mid*1.0001,
        "obi_5": (2*.9999-1.0001)/(2*.9999+1.0001),
        "return_15": .002, "robust_sigma_1800": 1.4826*.0005,
        "shock_z_1800": .0015/(1.4826*.0005), "buy_usdc_60": 900.,
        "sell_usdc_60": 300., "tfi_60": .5, "cvd_60": 600.,
        "persistence_long": .75, "persistence_short": .25, "rvol_60": None,
    }.items():
        check(key, results[key]["value"], expected)


def test_forward_path_costs_and_visible_quantity():
    config = ResearchParameters(notional_usdc=101, taker_fee_bps=5, slippage_bps=1)
    analyzer = ForwardReturnAnalyzer(config)
    origin = (100, 200, {"px": "99", "sz": "2"}, {"px": "101", "sz": "2"})
    end = (105, 200, {"px": "104", "sz": "1"}, {"px": "106", "sz": "2"})
    check("default_horizons", config.horizons_s, [5, 15, 30, 60, 180, 300, 900, 1800])
    for direction, sign in [("LONG", 1), ("SHORT", -1)]:
        result = analyzer.label(origin, end, [(1, quote(95)), (2, end)], direction, 2)
        check(direction+" return", result["return_log"], sign*math.log(1.05))
        check(direction+" MFE", result["mfe"], math.log(1.05) if sign == 1 else -math.log(.95))
        check(direction+" MAE", result["mae"], math.log(.95) if sign == 1 else -math.log(1.05))
        check(direction+" path coverage", result["coverage"], 1.)
    execution = analyzer.execution(origin, end, 1)
    check("long BBO entry", float(execution["entry"]), 101.0101)
    check("long BBO exit", float(execution["exit"]), 103.9896)
    check("long BBO fees", float(execution["fees_usdc"]), .10249985)
    check("long BBO net", float(execution["net_usdc"]), 2.87700015)
    check("matched gross", float(execution["gross_usdc"]), 5.)
    check("spread cost", float(execution["spread_usdc"]), 2.)
    check("slippage cost", float(execution["slippage_usdc"]), .0205)
    check("insufficient depth", ForwardReturnAnalyzer(
        config.model_copy(update={"notional_usdc": 102})).execution(origin, end, 1)["status"], "insufficient_depth")
    check("missing", analyzer.label(origin, None, [], "LONG", 2)["status"], "MISSING")
    check("partial path MFE", analyzer.label(origin, end, [(2, end)], "LONG", 2)["mfe"], None)


@pytest.mark.parametrize(("field", "value", "reason", "confirmed"), [
    ("tfi_15", .499, "tfi", "A"), ("tfi_60", .299, "tfi", "A"),
    ("rvol_30", 1.999, "rvol", "AB"), ("persistence_long", .749, "persistence", "ABC"),
    ("trades_30", 19, "trade_count", ""), ("spread_bps", 8.001, "spread", ""),
    ("unknown_usdc_60", 50.001, "unknown_side", ""),
    ("return_60", .008001, "price_chase", ""),
])
def test_s01_reasons_and_distinct_ablation(field, value, reason, confirmed):
    inputs = dict(healthy=True,warmup=True,mid=100,return_60=.0005,spread_bps=8,trades_30=20,
                  tfi_15=.5,tfi_60=.3,rvol_30=2.,volume_60=1000,unknown_usdc_60=50.,
                  persistence_long=.75,persistence_short=0.)
    inputs[field] = value
    events = S01(ResearchParameters()).on_tick(IID, BASE, {
        name: dict(value=entry,coverage=1.) for name,entry in inputs.items()
    })
    check(field+" confirmed", "".join(e["variant"] for e in events if e["state"]=="CONFIRMED"), confirmed)
    check(field+" D reason", events[-1]["reasons"], [reason])


def test_disconnect_out_of_order_and_stale_quote():
    engine = FeatureEngine(ResearchParameters())
    features = {}
    for t in range(6):
        engine.advance(BASE + t*1000)
        engine.on_event(row("health", {"connected": t != 5}, t))
        if t in (1, 2):
            engine.on_event(row("bbo", {"bbo":list(quote(100 if t==1 else 200)[2:])}, t,
                                lag=1500 if t==2 else 0))
        engine.seal(IID)
        features[t], _ = engine.snapshot(IID, BASE+t*1000)
    check("out of order", engine.quality["out_of_order"], 1)
    check("old quote rejected", features[2]["mid"]["value"], 100.)
    check("age=2000 ms", features[3]["mid"]["value"], 100.)
    check("age=3000 ms", features[4]["mid"]["value"], None)
    check("disconnected volume", features[5]["volume_1"]["value"], None)
