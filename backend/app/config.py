import os
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://quant:quant@localhost:5432/quant")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT.parent / "data"))
SNAPSHOT_SECONDS = max(60, int(os.getenv("SNAPSHOT_SECONDS", "60")))
BOOTSTRAP_BARS = min(500, max(30, int(os.getenv("BOOTSTRAP_BARS", "120"))))
HISTORY_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")
MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
if os.getenv("PAPER_ONLY", "true").lower() != "true":
    raise RuntimeError("PAPER_ONLY must be true")

