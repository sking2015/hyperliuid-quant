import os
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app.api.main import app
from app.exchange.hyperliquid.public import candle, perp_rows, spot_rows
from app.market_data.repository import catalog, now, record_gaps, save_candles
from app.market_data.stream import subscription_refs
from app.storage import db as storage


@pytest.fixture
def database(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL for PostgreSQL integration tests")
    schema = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        monkeypatch.setattr(storage, "DATABASE_URL", make_conninfo(url, options=f"-c search_path={schema}"))
        try:
            with TestClient(app) as client:
                yield client
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_pool_persistence_refresh_and_references(database, raw):
    client = database
    prefix = "/api/v1"
    stamp = now()
    tokens, spots = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "SPOT", "", spots, stamp)
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
    assert client.get(prefix + "/health").json()["status"] == "ok"
    markets = client.get(prefix + "/markets?q=BTC&market_type=PERP").json()["items"]
    assert [row["symbol"] for row in markets] == ["BTC"]
    assert client.get(prefix + "/markets?sort=bad").json()["request_id"]
    manual = client.post(prefix + "/watchlists", json={"name": "手动"}).json()
    route = prefix + "/watchlists/" + manual["id"]
    assert client.post(route + "/members/PERP::0").json()["changed"]
    assert not client.post(route + "/members/PERP::0").json()["changed"]
    pool = client.post(prefix + "/watchlists", json={"name": "Top 10", "type": "DYNAMIC"}).json()
    dynamic = prefix + "/watchlists/" + pool["id"]
    assert client.post(dynamic + "/preview").json()["results"][0]["instrument_id"] == "PERP::0"
    assert client.get(dynamic).json()["members"] == []
    assert client.post(dynamic + "/refresh").status_code == 200
    assert client.get(dynamic + "/history").json()[0]["results"][0]["action"] == "ENTERED"
    assert subscription_refs()[1]["BTC"] == 2
    assert client.delete(route + "/members/PERP::0").status_code == 200
    assert subscription_refs()[1]["BTC"] == 1
    assert client.post(dynamic + "/members/PERP::0", json={"blocked": True, "pinned": True}).status_code == 200
    assert not subscription_refs()[1]
    assert client.get(prefix + "/system/status").json()["observed"] == 0
    with TestClient(app) as restarted:
        assert len(restarted.get(prefix + "/watchlists").json()) == 2
        with restarted.websocket_connect(prefix + "/ws/events") as socket:
            assert socket.receive_json()["seq"] == 1


def test_candle_dedup_and_coverage(database, raw):
    client = database
    stamp = now()
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
        bar = candle(raw["candle"], "PERP::0", stamp + timedelta(days=365), "rest")
        assert bar["is_closed"]
        save_candles(db, [bar, bar])
        save_candles(db, [dict(bar, is_closed=False, o=0, received_ts=bar["received_ts"] + timedelta(seconds=1))])
        record_gaps(db, "PERP::0", "1m", bar["open_ts"], bar["open_ts"] + timedelta(minutes=2))
    route = "/api/v1/markets/PERP::0"
    response = client.get(route + "/candles").json()
    assert len(response) == 1 and response[0]["is_closed"] and response[0]["o"] == 60178.0
    assert len(client.get(route + "/coverage").json()["gaps"]) == 1


def test_parquet_manifest_round_trip(database, raw, tmp_path):
    import duckdb

    from app.market_data.archive import archive_day, checksum

    stamp = now()
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
        bar = candle(raw["candle"], "PERP::0", stamp + timedelta(days=365), "rest")
        day = (stamp - timedelta(days=1)).date()
        old = bar["open_ts"].replace(year=day.year, month=day.month, day=day.day)
        save_candles(db, [dict(bar, open_ts=old, close_ts=old + timedelta(seconds=59, milliseconds=999))])
    [partition] = archive_day(day, tmp_path)
    assert archive_day(day, tmp_path) == [partition]
    with storage.connect() as db:
        row = db.execute("SELECT * FROM market_data_manifest").fetchone()
        assert row["checksum"] == checksum(partition) and row["row_count"] == 1
    with duckdb.connect() as data:
        assert data.read_parquet(str(partition)).select("c").fetchone() == ("60200.0",)

def test_local_csv_import_uses_source_and_keeps_rest(database, raw, tmp_path):
    from app.market_data.import_candles import import_csv

    stamp = now()
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
    path = tmp_path / "candles.csv"
    path.write_text("open_ts,open,high,low,close,volume,trades\n"
                    "2026-01-01T01:00:00Z,100,104,99,102,2.1,5\n"
                    "2026-01-01T01:02:00Z,100,104,99,102,2.1,5\n", encoding="utf-8")
    assert import_csv(path, "PERP::0", "1m", "test-source") == 2
    assert import_csv(path, "PERP::0", "1m", "test-source") == 2
    with storage.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM candle_imports").fetchone()["n"] == 1
        bar = db.execute("SELECT * FROM candles ORDER BY open_ts LIMIT 1").fetchone()
        save_candles(db, [dict(bar, source="rest", c=103)])
    new_file = tmp_path / "different.csv"
    new_file.write_text(path.read_text().replace("102", "101"), encoding="utf-8")
    import_csv(new_file, "PERP::0", "1m", "test-source")
    with storage.connect() as db:
        rows = db.execute("SELECT * FROM candles ORDER BY open_ts").fetchall()
        assert rows[0]["source"] == "rest" and rows[0]["c"] == 103
        assert rows[1]["source"] == "import:test-source"
        assert db.execute("SELECT count(*) AS n FROM data_gaps").fetchone()["n"] == 1


def test_funding_settlement_idempotency(database, raw):
    from app.market_data.funding import next_funding_job, save_funding

    stamp = now()
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
    job = next_funding_job()
    event = {"coin": "BTC", "time": 1795485600049, "fundingRate": "0.0000077308", "premium": "-0.0004381532"}
    for _ in range(2):
        save_funding(job, [event], event["time"] - 1, event["time"] + 1, stamp)
    with storage.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM funding_rates").fetchone()["n"] == 1

def test_import_job_validation_execution_and_origin(database, raw, tmp_path, monkeypatch):
    from app.market_data import import_jobs

    client = database
    stamp = now()
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
    root = tmp_path / "imports"
    root.mkdir()
    (root / "bars.csv").write_text("open_ts,open,high,low,close,volume,trades\n"
                                   "2026-01-01T01:00:00Z,100,104,99,102,2.1,5\n",
                                   encoding="utf-8")
    monkeypatch.setattr(import_jobs, "DATA_DIR", tmp_path)
    route = "/api/v1/data/import-jobs"
    payload = {"instrument_id": "PERP::0", "filename": "bars.csv", "interval": "1m", "source": "fixture"}
    assert client.post(route, json=payload, headers={"Origin": "https://outside.example"}).status_code == 403
    assert client.post(route, json=dict(payload, filename="../bars.csv")).status_code == 422
    headers = {"Idempotency-Key": "sample-import"}
    request = client.post(route, json=payload, headers=headers)
    assert request.status_code == 202
    job = request.json()
    assert client.post(route, json=payload, headers=headers).json()["id"] == job["id"]
    assert client.post(route, json=dict(payload, interval="3m"), headers=headers).status_code == 409
    import_jobs.run_one_job()
    status = client.get(route + "/" + job["id"]).json()
    assert status["status"] == "DONE" and status["row_count"] == 1

def test_bbo_archive_preserves_exchange_and_receive_times(database, raw, tmp_path):
    import duckdb

    from app.market_data.archive import archive_day
    from app.market_data.stream import write_messages

    stamp = now()
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), stamp)
    received = stamp - timedelta(days=1)
    write_messages([(raw["ws_bbo"], received)], {"BTC": "PERP::0"})
    [partition] = archive_day(received.date(), tmp_path, "bbo")
    with duckdb.connect() as data:
        record = data.read_parquet(str(partition)).select("event_type, payload, epoch_ms(exchange_ts)").fetchone()
        assert record[0] == "bbo" and '"px": "60178"' in record[1]
        assert record[2] == raw["ws_bbo"]["data"]["time"]
