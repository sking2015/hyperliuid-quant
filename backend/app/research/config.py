from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FEATURE_VERSION = "1.0.0"
DETECTOR_VERSION = "1.2.0"
LABEL_VERSION = "1.1.0"


class ResearchParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    warmup_s: int = Field(3600, ge=60, le=7200)
    baseline_s: int = Field(3600, ge=60, le=7200)
    min_coverage: float = Field(.90, gt=0, le=1)
    max_unknown: float = Field(.05, ge=0, le=1)
    bbo_age_ms: int = Field(2000, ge=100, le=30000)
    l2_age_ms: int = Field(3000, ge=100, le=30000)
    late_ms: int = Field(2000, ge=0, le=30000)
    health_age_ms: int = Field(2500, ge=1000, le=10000)
    min_trade_buckets: int = Field(9, ge=1, le=12)
    tfi15: float = Field(.5, ge=0, le=1)
    tfi60: float = Field(.3, ge=0, le=1)
    rvol30: float = Field(2, ge=0)
    persistence: float = Field(.75, ge=0, le=1)
    return_min: float = Field(.0005, ge=0)
    return_max: float = Field(.008, gt=0)
    spread_bps: float = Field(8, gt=0)
    trades30: int = Field(20, ge=1)
    cooldown_s: int = Field(60, ge=1, le=3600)
    horizons_s: list[int] = Field(default_factory=lambda: [5,15,30,60,180,300,900,1800], min_length=1, max_length=12)
    variants: list[Literal["A","B","C","D"]] = Field(default_factory=lambda: ["A","B","C","D"], min_length=1)
    notional_usdc: float = Field(100, gt=0, le=1_000_000)
    taker_fee_bps: float = Field(5, ge=0, le=100)
    slippage_bps: float = Field(1, ge=0, le=100)
    epsilon: float = Field(.0001, gt=0)
    impact_floor: float = Field(.01, gt=0)
    match_window_s: int = Field(1800, ge=60, le=14400)
    match_vol_bps: float = Field(2, gt=0)
    match_spread_bps: float = Field(2, gt=0)
    bootstrap_seed: int = Field(42, ge=0)
    bootstrap_min_blocks: int = Field(30, ge=2)
    bootstrap_iterations: int = Field(2000, ge=100, le=5000)
    reference_asset: str | None = None
    factor_alpha: float = 0
    factor_beta: float | None = None
    factor_available_ms: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def bounds(self):
        if self.return_min > self.return_max or any(not 1 <= h <= 3600 for h in self.horizons_s):
            raise ValueError("Invalid return range or label horizon (1–3600 seconds)")
        if self.factor_beta is not None and (not self.reference_asset or self.factor_available_ms is None):
            raise ValueError("Factor coefficients require a reference and publication time")
        self.horizons_s = sorted(set(self.horizons_s))
        self.variants = sorted(set(self.variants))
        return self

    def fingerprint(self, detector_id="S01", detector_version=DETECTOR_VERSION):
        payload = [detector_id, FEATURE_VERSION, detector_version, LABEL_VERSION, self.model_dump(mode="json")]
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

