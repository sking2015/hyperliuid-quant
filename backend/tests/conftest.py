import json
from pathlib import Path

import pytest


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parent / "fixtures" / "hyperliquid.json").read_text(encoding="utf8"))

