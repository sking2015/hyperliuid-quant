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

def test_backtest_async_queue_repeatability_and_ledger(database, raw):
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.workers.backtest import run_once

    client = database
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    step = timedelta(minutes=1)
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), now())
        bars = []
        for index in (0, 1, 2, 3):
            stamp = start + step * index
            bars.append(dict(instrument_id="PERP::0", interval="1m", open_ts=stamp,
                             close_ts=stamp + step - timedelta(milliseconds=1), o=100, h=105 if index == 2 else 101,
                             l=99, c=100, v=1, trades=1, source="fixture", is_closed=True, received_ts=now()))
        save_candles(db, bars)
    route = "/api/v1/backtests"
    data = dict(strategy_id="deterministic", instrument_ids=["PERP::0"],
                parameters={"timeframe": "1m"}, start=start.isoformat(), end=(start + 4*step).isoformat())
    assert client.post(route + "/preflight", json=data).json()["ready"]
    coverage = client.get("/api/v1/markets/PERP::0/coverage", params={
        "start": start.isoformat(), "end": (start + 5*step).isoformat(), "interval": "1m",
    }).json()
    assert coverage["available"] == 4 and coverage["missing"] == 1
    cancelled = client.post(route, json=data).json()["id"]
    assert client.post(f"{route}/{cancelled}/cancel").json()["status"] == "CANCELLED"
    reports = []
    for _ in range(2):
        response = client.post(route, json=data)
        assert response.status_code == 202, response.text
        path = f"{route}/{response.json()['id']}"
        assert run_once()
        status = client.get(path).json()
        assert status["status"] == "SUCCEEDED", status
        report = client.get(path + "/report").json()
        ledger = client.get(path + "/ledger").json()
        assert abs(Decimal("1000") + sum(Decimal(entry["amount"]) for entry in ledger)
                   - Decimal(report["metrics"]["final_cash"])) < Decimal("1e-20")
        assert report["metrics"]["trade_count"] > 0
        assert client.get(path + "/trades.csv").status_code == 200
        reports.append(report)
    assert reports[0] == reports[1]

def test_backtest_freezes_observation_pool_membership(database, raw):
    from datetime import datetime, timezone
    from app.workers.backtest import run_once

    client = database
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    tokens, _ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], tokens, "", 0), now())
        save_candles(db, [dict(instrument_id="PERP::0", interval="1m", open_ts=start,
                              close_ts=end - timedelta(milliseconds=1), o=100, h=100,
                              l=100, c=100, v=1, trades=1, source="fixture", is_closed=True,
                              received_ts=now())])
    pool = client.post("/api/v1/watchlists", json={"name": "Frozen test"}).json()
    member = f"/api/v1/watchlists/{pool['id']}/members/PERP::0"
    assert client.post(member, json={}).status_code == 200
    created = client.post("/api/v1/backtests", json=dict(strategy_id="deterministic",
                          watchlist_id=pool["id"], parameters={"timeframe": "1m"},
                          start=start.isoformat(), end=end.isoformat()))
    assert created.status_code == 202, created.text
    assert client.delete(member).status_code == 200
    assert run_once()
    task = client.get("/api/v1/backtests/" + created.json()["id"]).json()
    assert task["status"] == "SUCCEEDED"
    assert task["configuration"]["watchlist"]["id"] == pool["id"]
    assert task["configuration"]["instrument_ids"] == ["PERP::0"]

def test_research_default_range_uses_longest_shared_continuous_history(database, raw):
    from datetime import datetime, timezone

    from app.workers.research import Runner

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    epoch = int(start.timestamp() * 1000)
    meta, contexts = raw["perp"]
    meta["universe"].append({**meta["universe"][0], "name": "ETH"})
    contexts.append(contexts[0])
    tokens, _ = spot_rows(*raw["spot"])
    btc, eth = "PERP::0", "PERP::1"
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(meta, contexts, tokens, "", 0), now())
        # BTC's newer connection is longer but all of its quotes arrive late. ETH starts later,
        # and an explicit gap during their overlap leaves a shorter shared span.
        for asset, first, last in [(btc, 0, 360), (btc, 500, 1000), (eth, 30, 360)]:
            db.execute("""
                INSERT INTO market_events(event_id,instrument_id,event_type,received_ts,source,schema_version,
                    payload,available_ts,ingest_seq,connection_id,exchange_ts)
                SELECT %s || ':' || kind || ':' || tick, %s, kind,tick,'fixture',3,
                    CASE kind WHEN 'research.health' THEN '{"connected":true}'::jsonb
                              WHEN 'research.bbo' THEN '{"bbo":[{"px":"100","sz":"1"},{"px":"101","sz":"1"}]}'::jsonb
                              ELSE '{"px":"100","sz":"1","aggressor":"buy"}'::jsonb END,
                    tick,nextval('research_ingest_seq'),%s,
                    CASE WHEN %s AND kind='research.bbo' THEN tick-interval '1 hour' ELSE tick END
                FROM generate_series(%s::timestamptz,%s::timestamptz,interval '1 second') AS tick
                CROSS JOIN unnest(ARRAY['research.health','research.bbo','research.trade']) AS kind
            """, (asset, asset, uuid4(), first == 500, start + timedelta(seconds=first), start + timedelta(seconds=last)))
        db.execute("INSERT INTO data_gaps VALUES (%s,'research.trade',%s,%s,'websocket_disconnect',false)",
                   (eth, start + timedelta(seconds=110), start + timedelta(seconds=160)))

    prefix = "/api/v1/research"
    quality = database.get(prefix + "/quality").json()
    assert quality["streams"] == []
    assert [asset["symbol"] for asset in quality["assets"]] == ["BTC", "ETH"]
    body = {"asset_ids": [btc], "parameters": {"warmup_s": 60, "baseline_s": 60, "horizons_s": [30]}}
    response = database.post(prefix + "/replay-range", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"start_ms": epoch + 120_000, "end_ms": epoch + 331_000}
    body["asset_ids"] = [btc, eth]
    assert database.post(prefix + "/replay-range", json=body).json() == {
        "start_ms": epoch + 280_000, "end_ms": epoch + 331_000,
    }
    config = database.post(prefix + "/configs", json=body["parameters"]).json()["hash"]
    draft = {"asset_ids": body["asset_ids"], "config_hash": config, "auto_range": True}
    result = database.post(prefix + "/experiments", json=draft)
    assert result.status_code == 202, result.text
    queued = result.json()
    assert queued["start_ms"] == epoch + 280_000
    assert database.get(prefix + "/runs?results=true").json() == []
    runner = Runner({**queued, "parameters": body["parameters"]})
    while not runner.advance(epoch + 360_000):
        pass
    completed = database.get(prefix + "/runs/" + queued["id"]).json()
    assert completed["status"] == "COMPLETE" and completed["stats"]["covered_ticks"] == 102
    body["parameters"]["baseline_s"] = 300
    assert database.post(prefix + "/replay-range", json=body).json() == {"start_ms": None, "end_ms": None}
    draft["config_hash"] = database.post(prefix + "/configs", json=body["parameters"]).json()["hash"]
    assert database.post(prefix + "/experiments", json=draft).status_code == 409
    assert len(database.get(prefix + "/runs").json()) == 1
    assert database.get(prefix + "/runs?results=true").json() == []


def test_research_fixture_to_event_label_api_and_archive(database, raw, tmp_path, monkeypatch):
    import hashlib
    from datetime import datetime, timezone
    from uuid import UUID

    import duckdb
    from psycopg.types.json import Jsonb

    from app.market_data.archive import archive_day
    from app.research import routes as research_routes
    from app.research.feed import normalize, health_rows
    from app.workers.research import Runner

    monkeypatch.setattr(research_routes,"DATA_DIR",tmp_path)

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    iid = "PERP::0"
    token_data, _ = spot_rows(*raw["spot"])
    connection = UUID(int=1)
    with storage.connect() as db:
        catalog(db, "PERP", "", perp_rows(*raw["perp"], token_data, "", 0), now())
        with db.cursor() as cursor:
            for offset in range(132):
                ts = start + timedelta(seconds=offset)
                tick_ms = int(ts.timestamp()*1000)
                mid = 100 if offset < 100 else 100.2 + (offset-100)*.001
                price = str(mid)
                events = list(health_rows({"BTC":iid},ts,connection,True))
                bbo = {"coin":"BTC","time":tick_ms,
                       "bbo":[{"px":str(mid-.01),"sz":"100"},{"px":str(mid+.01),"sz":"100"}]}
                trades = [{"coin":"BTC","time":tick_ms,"px":price,
                           "sz":"3" if offset>=71 else "1","side":"B","tid":offset}]
                for channel,data in (("bbo",bbo),("trades",trades)):
                    events.extend(normalize({"channel":channel,"data":data},{"BTC":iid},ts,connection))
                cursor.executemany("""
                    INSERT INTO market_events(event_id,instrument_id,event_type,exchange_ts,received_ts,payload,
                        connection_id,quality,source,schema_version,available_ts,ingest_seq)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'fixture',2,%s,nextval('research_ingest_seq'))
                    ON CONFLICT DO NOTHING
                """,[(*event,ts) for event in events])
    [parquet] = archive_day(start.date(),tmp_path,"research.bbo")
    with duckdb.connect() as d:
        assert d.read_parquet(str(parquet)).count("*").fetchone()[0]==132
        assert d.read_parquet(str(parquet)).order("available_ts, ingest_seq").limit(1).select("instrument_id").fetchone()==(iid,)
    root = "/api/v1/research"
    params = {"warmup_s":60,"baseline_s":60,"horizons_s":[5,15]}
    config = database.post(root+"/configs",json=params).json()
    epoch = int(start.timestamp()*1000)
    result = database.post(root+"/experiments",json={"asset_ids":[iid],"mode":"REPLAY","start_ms":epoch+100000,
                           "end_ms":epoch+130000,"config_hash":config["hash"]})
    assert result.status_code==202,result.text
    run_id=result.json()["id"]
    with storage.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM research_data_pins WHERE run_id=%s",(run_id,)).fetchone()["n"]==1
        job=db.execute("SELECT r.*,c.parameters FROM research_runs r JOIN research_configs c ON c.hash=r.config_hash WHERE r.id=%s",(run_id,)).fetchone()
    for _ in range(2):
        task=Runner(job,archive_root=tmp_path)
        while not task.advance(epoch+145000):
            pass
    verified=database.get(root+"/runs/"+run_id).json()
    with storage.connect() as db:
        db.execute("DELETE FROM market_events WHERE event_type='research.bbo'")
    task=Runner(job,archive_root=tmp_path)
    while not task.advance(epoch+145000):
        pass
    final=database.get(root+"/runs/"+run_id).json()
    assert verified["source_hash"]==final["source_hash"]
    assert final["status"]=="COMPLETE" and final["stats"]["covered_ticks"]>0
    events=database.get(root+"/events",params={"run_id":run_id,"state":"CONFIRMED"}).json()
    assert {row["variant"] for row in events}=={"A","B","C","D"}
    assert len(events)==4 and len({row["episode_id"] for row in events})==1
    detail=database.get(root+"/events/"+events[0]["id"]).json()
    assert [v["status"] for v in detail["labels"]]==["COMPLETE","COMPLETE"]
    assert detail["labels"][0]["detail"]["execution"]["status"]=="filled"
    window=database.get(root+"/events/"+events[0]["id"]+"/replay").json()["rows"]
    assert any(row["event_type"]=="research.bbo" for row in window)
    retention=database.get(root+"/quality/retention").json()
    assert retention["dry_run"] and retention["partitions"][0]["pinned"]
    assert not retention["partitions"][0]["archive_purge_candidate"]
    assert all(row["attempts"]==1 for row in database.get(root+"/forward-returns",params={"run_id":run_id}).json())


def test_replay_matures_labels_after_detection_end(database, raw, tmp_path, monkeypatch):
    from app.workers.research import Runner

    test_research_fixture_to_event_label_api_and_archive(database, raw, tmp_path, monkeypatch)
    root = "/api/v1/research"
    fixture_run = database.get(root+"/runs").json()[0]
    start = fixture_run["start_ms"]
    end = start+5000
    response = database.post(root+"/runs",json={"asset_ids":["PERP::0"],"mode":"REPLAY",
        "start_ms":start,"end_ms":end,"config_hash":fixture_run["config_hash"]})
    assert response.status_code == 202
    run_id = response.json()["id"]
    with storage.connect() as db:
        job = db.execute("""
            SELECT r.*,c.parameters FROM research_runs r
            JOIN research_configs c ON c.hash=r.config_hash WHERE r.id=%s
        """,(run_id,)).fetchone()
    runner = Runner(job,archive_root=tmp_path)
    while runner.cursor < start:
        assert not runner.advance(start)
    events = database.get(root+"/events",params={"run_id":run_id,"state":"CONFIRMED"}).json()
    assert len(events) == 4 and {event["detected_ms"] for event in events} == {start}
    detail = root+"/events/"+events[0]["id"]
    assert [(l["horizon_s"],l["status"]) for l in database.get(detail).json()["labels"]] == [(5,"PENDING"),(15,"PENDING")]
    # Restart the runner with the same job ID. Both targets have BBO input in the fixture.
    runner = Runner(job,archive_root=tmp_path)
    while not runner.advance(end+15000):
        pass
    labels = database.get(detail).json()["labels"]
    actual = [(l["horizon_s"],l["status"]) for l in labels]
    assert actual == [(5,"COMPLETE"),(15,"COMPLETE")]
    with storage.connect() as db:
        assert not db.execute("SELECT 1 FROM research_events WHERE run_id=%s AND detected_ms>=%s",(run_id,end)).fetchone()
    assert database.get(root+"/runs/"+run_id).json()["input_sequence"] == job["input_sequence"]

    stopped = database.post(root+"/runs",json={"asset_ids":["PERP::0"],"mode":"REPLAY",
        "start_ms":start,"end_ms":end,"config_hash":fixture_run["config_hash"]}).json()
    with storage.connect() as db:
        stopped_job = db.execute("""
            SELECT r.*,c.parameters FROM research_runs r JOIN research_configs c ON c.hash=r.config_hash WHERE r.id=%s
        """,(stopped["id"],)).fetchone()
    runner = Runner(stopped_job,archive_root=tmp_path)
    while runner.cursor < start:
        assert not runner.advance(start)
    assert runner.advance(start,stopping=True)
    event = database.get(root+"/events",params={"run_id":stopped["id"],"state":"CONFIRMED"}).json()[0]
    labels = database.get(root+"/events/"+event["id"]).json()["labels"]
    assert all(item["status"]=="CENSORED" and item["detail"]["reason"]=="task_stopped" for item in labels)
    assert database.get(root+"/forward-returns",params={"run_id":stopped["id"]}).json()==[]
    assert database.get(root+"/forward-returns",params={"run_id":stopped["id"],"diagnostic":True}).json()

    empty = database.post(root+"/runs",json={"asset_ids":["PERP::0"],"mode":"REPLAY",
        "start_ms":start-86400000,"end_ms":end-86400000,"config_hash":fixture_run["config_hash"]}).json()
    assert empty["status"]=="INSUFFICIENT_DATA"
    assert {issue["reason"] for issue in empty["preflight"]["issues"]}=={"no_records"}
    assert {run["id"] for run in database.get(root+"/runs?results=true").json()} == {run_id, fixture_run["id"]}

    from app.research.store import stamp
    with storage.connect() as db:
        db.execute("""
            INSERT INTO market_events(event_id,instrument_id,event_type,received_ts,available_ts,
                 source,schema_version,payload,connection_id,quality,ingest_seq)
            SELECT 'missing-target',instrument_id,event_type,received_ts,available_ts,source,schema_version,
                   '{"connected":false}',connection_id,'[]',nextval('research_ingest_seq')
            FROM market_events WHERE event_type='research.health' AND available_ts=%s
        """,(stamp(start+5000),))
    config = database.post(root+"/configs",json={"warmup_s":60,"baseline_s":60,
                                                "horizons_s":[5,60]}).json()
    incomplete = database.post(root+"/runs",json={"asset_ids":["PERP::0"],"mode":"REPLAY",
        "start_ms":start,"end_ms":end,"config_hash":config["hash"]}).json()
    with storage.connect() as db:
        incomplete_job = db.execute("""
            SELECT r.*,c.parameters FROM research_runs r JOIN research_configs c ON c.hash=r.config_hash WHERE r.id=%s
        """,(incomplete["id"],)).fetchone()
    runner = Runner(incomplete_job,archive_root=tmp_path)
    while not runner.advance(runner.label_end):
        pass
    assert database.get(root+"/runs/"+incomplete["id"]).json()["status"]=="INSUFFICIENT_DATA"
    event = database.get(root+"/events",params={"run_id":incomplete["id"],"state":"CONFIRMED"}).json()[0]
    labels = database.get(root+"/events/"+event["id"]).json()["labels"]
    assert [(item["status"],item["detail"]["reason"]) for item in labels] == [
        ("MISSING","missing_or_stale_target_bbo"),("CENSORED","source_window_ended")]


def test_research_publication_recovers_committed_staging_and_preserves_clock_order(database, raw):
    from datetime import datetime, timezone
    from threading import Event, Thread

    from app.research.feed import persist
    from app.research.store import feed_rows

    token_data,_ = spot_rows(*raw["spot"])
    with storage.connect() as db:
        catalog(db,"PERP","",perp_rows(*raw["perp"],token_data,"",0),now())
    iid = "PERP::0"
    past = datetime(2026,1,1,tzinfo=timezone.utc)
    future = now()+timedelta(minutes=1)
    start = int(past.timestamp()*1000)
    end = int(future.timestamp()*1000)+1000
    inserted = Event()
    release = Event()
    failures = []

    def reader_side_writer():
        try:
            with storage.connect() as writer:
                writer.execute("""
                    INSERT INTO market_events(event_id,instrument_id,event_type,exchange_ts,received_ts,
                         source,schema_version,payload,connection_id,quality)
                    VALUES ('staged-late',%s,'research.trade',%s,%s,'hyperliquid_research_ws_v3',
                            3,'{}',%s,'[]')
                """,(iid,past,future,uuid4()))
                inserted.set()
                assert release.wait(10)
        except Exception as exc:
            failures.append(exc)

    thread = Thread(target=reader_side_writer)
    thread.start()
    try:
        assert inserted.wait(10)
        # A second writer commits out of exchange order while the first transaction is still open.
        from psycopg.types.json import Jsonb
        current = now()
        persist([("newer",iid,"research.trade",current,current,Jsonb({}),uuid4(),Jsonb([]))],{})
        with storage.connect() as reader:
            assert [r["event_id"] for r in feed_rows(reader,[iid],start,end)]==["newer"]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not failures
    with storage.connect() as reader:
        assert [r["event_id"] for r in feed_rows(reader,[iid],start,end)]==["newer"]
    persist([],{})
    with storage.connect() as reader:
        rows = feed_rows(reader,[iid],start,end)
        assert [r["event_id"] for r in rows]==["newer","staged-late"]
        assert rows[1]["available_ts"]==future and rows[1]["ingest_seq"]>rows[0]["ingest_seq"]
        assert [r["event_id"] for r in feed_rows(reader,[iid],start,end,rows[0]["ingest_seq"])]==["newer"]
