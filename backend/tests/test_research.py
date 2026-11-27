import json
from datetime import datetime, timedelta, timezone
from math import log
from pathlib import Path
from uuid import UUID

import pytest

from app.research.config import ResearchParameters
from app.research.detectors import S01
from app.research.feed import normalize
from app.research.features import FeatureEngine
from app.research.labels import ForwardReturnAnalyzer

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
BASE = int(EPOCH.timestamp())*1000
IID = "PERP::0"
CONNECTION = UUID(int=1)


def row(kind, payload, tick, lag=0):
    available = EPOCH + timedelta(seconds=tick)
    return dict(instrument_id=IID,event_type="research."+kind,available_ts=available,received_ts=available,
                exchange_ts=available-timedelta(milliseconds=lag),connection_id=CONNECTION,
                quality=[],payload=payload)


def quote(price):
    return price,2.,{"px":str(price-.01),"sz":"100"},{"px":str(price+.01),"sz":"100"}


def test_mainnet_l2_fixture_preserves_precision_and_limits():
    raw = json.loads((Path(__file__).parent/"fixtures/hyperliquid_l2_mainnet.json").read_text())
    [event] = list(normalize(raw,{"BTC":IID},EPOCH,CONNECTION))
    assert event[1:3] == (IID,"research.l2")
    assert event[4]==EPOCH
    assert event[5].obj["levels"]==raw["data"]["levels"]
    assert event[5].obj["depth_limit"] == [20,20]
    assert event[7].obj == ["finite_depth"]


def test_feature_boundaries_late_and_unknown():
    engine = FeatureEngine(ResearchParameters(warmup_s=60,baseline_s=60))
    for tick in range(1,4):
        engine.advance(BASE+tick*1000)
        engine.on_event(row("health",{"connected":True},tick))
        engine.on_event(row("bbo",{"bbo":[{"px":"99","sz":"1"},{"px":"101","sz":"1"}]},tick))
        if tick==2:
            for side, flow in (("buy","75"),("sell","25")):
                engine.on_event(row("trade",{"aggressor":side,"px":flow,"sz":"1"},tick))
        if tick==3:
            engine.on_event(row("trade",{"aggressor":"buy","px":"1000","sz":"1"},tick,lag=3000))
        engine.seal(IID)
        features,_ = engine.snapshot(IID,BASE+tick*1000)
        if tick==2:
            assert features["tfi_1"]["value"] == .5 and features["mid"]["value"]==100
        if tick==3:
            assert features["tfi_1"]["value"] is None and engine.quality["late_or_future"]==1
    with pytest.raises(ValueError,match="Future"):
        engine.on_event(row("trade",{"px":"1","sz":"1"},4))


def test_volume_baseline_excludes_current_and_book_truncation():
    engine = FeatureEngine(ResearchParameters(warmup_s=60,baseline_s=60))
    for tick in range(1,95):
        stamp = BASE+tick*1000
        engine.advance(stamp)
        engine.on_event(row("health",{"connected":True},tick))
        engine.on_event(row("bbo",{"bbo":[{"px":"99.99","sz":"1"},{"px":"100.01","sz":"1"}]},tick))
        engine.on_event(row("trade",{"aggressor":"buy","px":"100","sz":"1" if tick<65 else "3"},tick))
        if tick==94:
            engine.on_event(row("l2",{"levels":[[{"px":"99.99","sz":"2"}],[{"px":"100.01","sz":"1"}]]},tick))
        engine.seal(IID)
    f,_ = engine.snapshot(IID,BASE+94000)
    assert f["rvol_30"]["value"]==3
    assert f["bid_depth_5"]["value"]==pytest.approx(199.98)
    assert not f["obi_5"]["valid"] and f["obi_5"]["quality_flags"]==["truncated_depth"]
    assert not f["robust_sigma_1800"]["valid"]


@pytest.mark.parametrize("direction,sign",[("LONG",1),("SHORT",-1)])
def test_labels_are_directional_and_stale_is_missing(direction,sign):
    analyzer = ForwardReturnAnalyzer(ResearchParameters(notional_usdc=100,taker_fee_bps=5,slippage_bps=1))
    q = quote(100)
    end = quote(101)
    value = analyzer.label(q,end,[(1,quote(99)),(2,end)],direction,2)
    assert value["return_log"]==pytest.approx(sign*log(1.01))
    assert value["mfe"]>0 and value["mae"]<0
    assert value["execution"]["status"]=="filled"
    assert analyzer.label(q,None,[],direction,2)["status"]=="MISSING"
    shallow = (end[0],end[1],{"px":"100","sz":".0001"},{"px":"101","sz":".0001"})
    assert analyzer.label(q,shallow,[(1,shallow)],direction,1)["execution"]["status"]=="insufficient_depth"


def test_detector_cooldown_tracks_edges_and_checkpoints():
    detector = S01(ResearchParameters())
    values = {"healthy":True,"warmup":True,"mid":100,"return_60":.002,"spread_bps":2.,"trades_30":21,
              "tfi_15":.6,"tfi_60":.4,"rvol_30":3.,"volume_60":1000,"unknown_usdc_60":0.,
              "persistence_long":.8,"persistence_short":0.}
    sample=lambda: {k:dict(value=v,coverage=1.) for k,v in values.items()}
    hit = detector.on_tick(IID,BASE,sample())
    assert [e["variant"] for e in hit if e["state"]=="CONFIRMED"]==["A","B","C","D"]
    assert not detector.on_tick(IID,BASE+1000,sample())
    values["return_60"]=0
    detector.on_tick(IID,BASE+2000,sample())
    saved=detector.checkpoint()
    restored=S01(ResearchParameters())
    restored.restore(saved)
    values["return_60"]=.002
    rejected=detector.on_tick(IID,BASE+3000,sample())
    assert rejected==restored.on_tick(IID,BASE+3000,sample())
    assert all(item["reasons"]==["cooldown"] for item in rejected)
    assert not detector.on_tick(IID,BASE+61000,sample())


def test_s01_thresholds_mirror_short_and_reject_unknown_notional():
    detector = S01(ResearchParameters(variants=["D"]))
    values = {"healthy":True,"warmup":True,"mid":100,"return_60":-.0005,"spread_bps":8,
              "trades_30":20,"tfi_15":-.5,"tfi_60":-.3,"rvol_30":2.,"volume_60":1000,
              "unknown_usdc_60":50,"persistence_long":0.,"persistence_short":.75}
    sample = lambda: {key:dict(value=value,coverage=1.) for key,value in values.items()}
    [result] = detector.on_tick(IID,BASE,sample())
    assert result["state"]=="CONFIRMED" and result["direction"]=="SHORT"
    values["unknown_usdc_60"] = 50.1
    fresh = S01(ResearchParameters(variants=["D"]))
    [rejected] = fresh.on_tick(IID,BASE,sample())
    assert rejected["reasons"]==["unknown_side"]


def test_research_disk_hysteresis_preserves_m1_headroom(monkeypatch):
    from app.workers import depth

    monkeypatch.setattr(depth,"database_bytes",lambda:0)
    monkeypatch.setattr(depth,"data_file_bytes",lambda _:0)
    monkeypatch.setattr(depth,"WARN_FREE",100)
    monkeypatch.setenv("RESEARCH_RESUME_MIB","1")
    values=dict(disk_free_bytes=101,quota_remaining_bytes=10**9,host_probe_free_bytes=None,disk_alert="ok")
    monkeypatch.setattr(depth,"disk_status",lambda *_:values)
    assert depth.space_available()
    assert not depth.space_available(blocked=True)
    values["disk_free_bytes"]=2**20+101
    assert depth.space_available(blocked=True)
    values["disk_alert"]="warning"
    assert not depth.space_available()


def test_matched_controls_bootstrap_is_deterministic():
    from app.research.ablation import paired_summary

    config = ResearchParameters(match_window_s=60, bootstrap_min_blocks=2,
                                bootstrap_iterations=100, horizons_s=[5])
    rows = []
    for index, effect in enumerate((.01, .03)):
        data = dict(instrument_id=IID, direction="LONG", detected_ms=BASE+60000*index,
                    horizon_s=5, volatility=.001, spread=2)
        rows.extend((dict(data, id=f"A{index}", episode_id=f"control{index}", variant="A", return_log=.01),
                     dict(data, id=f"D{index}", episode_id=f"treatment{index}", variant="D", return_log=.01+effect)))
    result = paired_summary(rows,config,"D",5)
    assert (result["pairs"],result["blocks"],result["status"]) == (2,2,"EXPLORATORY")
    assert result["mean_difference"] == pytest.approx(.02)
    assert result["ci95"] == pytest.approx([.01,.03])
    assert result == paired_summary(rows,config,"D",5)
    assert paired_summary(rows[:2],config,"D",5)["ci95"] is None
