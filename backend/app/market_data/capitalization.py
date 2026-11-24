import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

CAP_FILE = os.getenv("MARKET_CAP_FILE")


def load_caps(as_of):
    if not CAP_FILE:
        return {}
    rows = json.loads(Path(CAP_FILE).read_text(encoding="utf-8"))
    available = {}
    for iid, row in rows.items():
        value = Decimal(str(row["market_cap_usd"]))
        stamp = datetime.fromisoformat(row["as_of"])
        source = row["source"].strip()
        if stamp.utcoffset() is None or not source or not value.is_finite() or value <= 0:
            continue
        if as_of - timedelta(hours=24) <= stamp <= as_of:
            available[iid] = {"market_cap": value, "cap_as_of": stamp, "cap_source": source}
    return available

