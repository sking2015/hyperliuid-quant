from collections import defaultdict
from hashlib import sha256
from time import perf_counter

from app.research.detectors import DETECTORS
from app.research.features import FeatureEngine
from app.research.labels import ForwardReturnAnalyzer


class ResearchEngine:
    def __init__(self, run_id, config, assets, detector_id="S01"):
        self.run_id = run_id
        self.config = config
        self.assets = tuple(assets)
        self.feature_assets = tuple(dict.fromkeys((*assets, *([config.reference_asset] if config.reference_asset else []))))
        self.features = FeatureEngine(config)
        self.detector = DETECTORS[detector_id](config)
        self.analyzer = ForwardReturnAnalyzer(config)
        self.pending = defaultdict(list)
        self.stats = defaultdict(int)
        self.feature_ms = 0.
        self.detection_ms = 0.
        self.asset_coverage = {iid: {"total":0,"covered":0} for iid in self.assets}
        self.parameter_hash = config.fingerprint(self.detector.id,self.detector.version)

    def on_tick(self, tick, rows, detect):
        started = perf_counter()
        self.features.advance(tick)
        for row in rows:
            self.features.on_event(row)
        for iid in self.feature_assets:
            self.features.seal(iid)
        self.feature_ms += (perf_counter()-started)*1000
        events, labels = [], []
        for iid in self.assets:
            started = perf_counter()
            values, quote = self.features.snapshot(iid,tick)
            self.feature_ms += (perf_counter()-started)*1000
            remaining = []
            for item in self.pending[iid]:
                elapsed = (tick - item["event"]["detected_ms"])//1000
                if quote:
                    item["path"].append((elapsed,quote))
                if elapsed in self.config.horizons_s:
                    labels.append((item["event"]["id"], elapsed, self.analyzer.label(
                        item["origin"], quote, item["path"], item["event"]["direction"], elapsed)))
                if elapsed < self.config.horizons_s[-1]:
                    remaining.append(item)
            self.pending[iid] = remaining
            if not detect:
                continue
            self.stats["asset_ticks"] += 1
            self.asset_coverage[iid]["total"] += 1
            if values["warmup"]["value"] and values["mid"]["valid"] and values["return_60"]["valid"] and values["rvol_30"]["coverage"] >= self.config.min_coverage:
                self.stats["covered_ticks"] += 1
                self.asset_coverage[iid]["covered"] += 1
            started = perf_counter()
            candidates = self.detector.on_tick(iid,tick,values)
            self.detection_ms += (perf_counter()-started)*1000
            for candidate in candidates:
                key = sha256(f"{self.run_id}:{self.parameter_hash}:{iid}:{candidate['direction']}:{candidate['variant']}:{tick}:{candidate['episode_id']}".encode()).hexdigest()
                event = dict(candidate,id=key,instrument_id=iid,detected_ms=tick,available_ms=tick,
                             detector_id=self.detector.id,detector_version=self.detector.version,
                             parameter_hash=self.parameter_hash,reference_mid=quote[0] if quote else None,
                             features=values, market_regime="UNKNOWN", liquidity_bucket="UNKNOWN")
                events.append(event)
                self.stats[candidate["state"].lower()] += 1
                if event["state"]=="CONFIRMED":
                    self.pending[iid].append(dict(event=event,origin=quote,path=[]))
                    labels.extend((key,h,{"status":"PENDING"}) for h in self.config.horizons_s)
        return events,labels

