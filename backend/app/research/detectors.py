from hashlib import sha256
from typing import Protocol

from app.research.config import DETECTOR_VERSION, ResearchParameters


class Detector(Protocol):
    id: str
    def on_tick(self, iid, tick, features): ...
    def checkpoint(self): ...
    def restore(self, state): ...


class S01:
    id = "S01"
    version = DETECTOR_VERSION
    parameters = ResearchParameters

    def __init__(self, config):
        self.config = config
        self.previous = {}
        self.triggered = {}
        self.episodes = {}

    def checkpoint(self):
        return self.previous.copy(), self.triggered.copy(), self.episodes.copy()

    def restore(self, state):
        self.previous, self.triggered, self.episodes = (item.copy() for item in state)

    def on_tick(self, iid, tick, features):
        p = self.config
        val = lambda key: features[key]["value"]
        if not val("healthy") or not val("warmup") or val("mid") is None:
            had_quality = self.previous.pop((iid,"quality"),False)
            for variant in (*p.variants, "candidate"):
                for side in ("LONG", "SHORT"):
                    self.previous[iid, variant, side] = False
            if had_quality:
                reason = "feed_gap" if not val("healthy") else "stale_quote" if val("mid") is None else "warmup_coverage"
                return [dict(variant=v,direction=d,episode_id=episode,state="REJECTED",reasons=[reason])
                        for v in p.variants for d in ("LONG","SHORT")
                        if (episode := self.episodes.get((iid,d),(None,None))[1])]
            return []
        self.previous[iid,"quality"] = True
        results = []
        for direction, sign in (("LONG",1),("SHORT",-1)):
            ret, spread = val("return_60"), val("spread_bps")
            momentum = ret is not None and sign*ret > 0 and p.return_min <= sign*ret <= p.return_max
            common = []
            if not momentum:
                common.append("price_chase" if ret is not None and sign*ret>p.return_max else "price_response")
            if spread is None or spread > p.spread_bps: common.append("spread")
            if val("trades_30") is None or val("trades_30") < p.trades30: common.append("trade_count")
            tfi15, tfi60, rvol = val("tfi_15"), val("tfi_60"), val("rvol_30")
            if features["rvol_30"]["coverage"] < p.min_coverage:
                common.append("baseline_coverage")
            if val("volume_60") and val("unknown_usdc_60") / val("volume_60") > p.max_unknown:
                common.append("unknown_side")
            flow = [] if tfi15 is not None and sign*tfi15 > 0 and sign*tfi15 >= p.tfi15 and tfi60 is not None and sign*tfi60 >= p.tfi60 else ["tfi"]
            relative = [] if rvol is not None and rvol >= p.rvol30 else ["rvol"]
            persistence = val("persistence_" + direction.lower())
            persistent = [] if persistence is not None and persistence >= p.persistence else ["persistence"]
            candidate_key = (iid, "candidate", direction)
            candidate = ret is not None and sign*ret >= p.return_min
            candidate_edge = candidate and not self.previous.get(candidate_key)
            self.previous[candidate_key] = candidate
            for variant in p.variants:
                key = (iid,variant,direction)
                reasons = common + (flow if variant >= "B" else []) + (relative if variant >= "C" else []) + (persistent if variant == "D" else [])
                eligible = not reasons
                edge = eligible and not self.previous.get(key)
                self.previous[key] = eligible
                if not edge and not (candidate_edge and not eligible):
                    continue
                group = iid,direction
                previous_tick, episode = self.episodes.get(group, (None, None))
                last = self.triggered.get(key)
                if previous_tick is None or tick - previous_tick >= p.cooldown_s*1000:
                    episode = sha256(f"{iid}:{direction}:{tick}".encode()).hexdigest()[:24]
                self.episodes[group] = tick, episode
                if eligible and last is not None and tick-last < p.cooldown_s*1000:
                    reasons = ["cooldown"]
                if not reasons:
                    self.triggered[key] = tick
                results.append(dict(variant=variant,direction=direction,episode_id=episode,
                                    state="REJECTED" if reasons else "CONFIRMED",reasons=reasons))
        return results


DETECTORS = {S01.id: S01}

