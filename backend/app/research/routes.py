import asyncio
from datetime import timedelta
from uuid import UUID, uuid4
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from app.config import DATA_DIR
from app.market_data.repository import now
from app.research.archive import ArchivedFeed
from app.research.config import FEATURE_VERSION
from app.research.detectors import DETECTORS
from app.research.features import ms
from app.research.feed import PUBLICATION_LOCK
from app.research.inputs import code_hash, replay_input, windows
from app.research.store import feed_rows, stamp
from app.storage.db import connect

router = APIRouter(prefix="/api/v1/research", tags=["research"])


@router.get("/detectors")
def detectors():
    return [{"id":detector.id,"version":detector.version,"feature_version":FEATURE_VERSION,
             "schema":detector.parameters.model_json_schema(),"defaults":detector.parameters().model_dump(),
             "dependencies":["BBO","trades"],"optional":["L2","asset_context"],"variants":["A","B","C","D"]}
            for detector in DETECTORS.values()]


@router.get("/quality")
def quality():
    with connect() as db:
        state = db.execute("SELECT heartbeat,state FROM worker_leases WHERE component='research_feed'").fetchone()
        rows = db.execute("""
            SELECT i.instrument_id,i.symbol,m.event_type,count(*) AS samples,min(m.available_ts) AS first,
                   max(m.available_ts) AS last
            FROM market_events m JOIN instruments i USING(instrument_id)
            WHERE m.ingest_seq IS NOT NULL AND m.available_ts > now()-interval '1 hour'
              AND m.event_type IN ('research.l2','research.bbo','research.trade')
            GROUP BY i.instrument_id,i.symbol,m.event_type ORDER BY i.symbol,m.event_type
        """).fetchall()
        stability = db.execute("""
            SELECT min(minute) AS first,max(minute) AS last,count(*) AS samples,
                   count(*) FILTER(WHERE state->>'connected'='true') AS connected_samples
            FROM research_stability_samples WHERE minute>=now()-interval '24 hours'
        """).fetchone()
        compute = db.execute("SELECT heartbeat,state FROM worker_leases WHERE component='research_compute'").fetchone()
    return {"collector":state,"streams":rows,"window_seconds":3600,"stability":stability,"compute":compute}


@router.post("/configs",status_code=201)
def config(payload: dict, detector_id: str = "S01"):
    detector = DETECTORS.get(detector_id)
    if detector is None:
        raise HTTPException(404,"未知研究检测器")
    try:
        parameters = detector.parameters.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    digest = parameters.fingerprint(detector.id,detector.version)
    with connect() as db:
        db.execute("""
            INSERT INTO research_configs(hash,detector_version,feature_version,parameters,detector_id)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """,(digest,detector.version,FEATURE_VERSION,Jsonb(parameters.model_dump(mode="json")),detector.id))
    return {"hash":digest,"detector_id":detector.id,"version":detector.version}


class Draft(BaseModel):
    config_hash: str = Field(min_length=64,max_length=64)
    mode: Literal["LIVE","REPLAY"] = "REPLAY"
    asset_ids: list[str] = Field(min_length=1,max_length=20)
    start_ms: int | None = None
    end_ms: int | None = None
    duration_s: int = Field(3600,ge=1,le=14400)


@router.post("/runs",status_code=202)
@router.post("/experiments",status_code=202)
def start(draft: Draft):
    current = ms(now())//1000*1000
    assets = sorted(set(draft.asset_ids))
    left,right = (current+1000,current+1000+draft.duration_s*1000) if draft.mode=="LIVE" else (draft.start_ms,draft.end_ms)
    if left is None or right is None or not 0 < right-left <= 14_400_000 or left < 0 or left%1000 or right%1000:
        raise HTTPException(422,"使用已对齐整秒的时间范围，最长四小时")
    if draft.mode=="REPLAY" and right > current:
        raise HTTPException(422,"回放终点必须已发生")
    with connect() as db:
        stored = db.execute("SELECT parameters FROM research_configs WHERE hash=%s",(draft.config_hash,)).fetchone()
        if not stored:
            raise HTTPException(404,"研究配置不存在")
        rows = db.execute("""
            SELECT instrument_id FROM instruments WHERE instrument_id=ANY(%s)
            AND market_type='PERP' AND quote='USDC'
        """,(assets,)).fetchall()
        if len(rows)!=len(assets):
            raise HTTPException(422,"只支持原生 USDC 永续")
        if draft.mode=="LIVE":
            health = db.execute("SELECT heartbeat,state FROM worker_leases WHERE component='research_feed'").fetchone()
            if not health or not health["state"].get("connected") or health["heartbeat"] < now()-timedelta(seconds=5):
                raise HTTPException(409,"研究采集当前未连接")
            available = db.execute("SELECT instrument_id FROM instruments WHERE market_type='PERP' AND asset_key=ANY(%s)",
                                   (health["state"]["assets"],)).fetchall()
            if not set(assets)<={r["instrument_id"] for r in available}:
                raise HTTPException(422,"实时研究只支持已配置持续采集的资产")
        params = stored["parameters"]
        warmup,follow = windows(left,right,params)
        input_assets = sorted(set(assets + ([params["reference_asset"]] if params.get("reference_asset") else [])))
        if draft.mode=="REPLAY":
            db.execute("SELECT pg_advisory_xact_lock(%s)",(PUBLICATION_LOCK,))
        sequence,watermark,preflight,pins = replay_input(
            db,input_assets,warmup,left,right,follow,max(params["horizons_s"])*1000,params["bbo_age_ms"])
        if draft.mode=="LIVE":
            sequence = watermark = None
        run = db.execute("""
            INSERT INTO research_runs(id,config_hash,mode,asset_ids,start_ms,end_ms,
                input_sequence,follow_end_ms,input_watermark_ms,preflight,code_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """,(uuid4(),draft.config_hash,draft.mode,assets,left,right,sequence,follow,watermark,Jsonb(preflight),code_hash())).fetchone()
        with db.cursor() as cursor:
            cursor.executemany("INSERT INTO research_data_pins VALUES (%s,%s) ON CONFLICT DO NOTHING",
                               [(run["id"],path) for path in pins])
        if draft.mode == "REPLAY" and preflight["blocking"]:
            run = db.execute("""
                UPDATE research_runs SET status='INSUFFICIENT_DATA', error=%s
                WHERE id=%s RETURNING *
            """,("历史行情预检未通过，详见缺失区间。",run["id"])).fetchone()
        return run


@router.get("/runs")
def runs(limit: int = Query(30,ge=1,le=100)):
    with connect() as db:
        return db.execute("SELECT * FROM research_runs ORDER BY created_at DESC,id DESC LIMIT %s",(limit,)).fetchall()


@router.get("/runs/{run_id}")
@router.get("/experiments/{run_id}")
def run_detail(run_id: UUID):
    with connect() as db:
        row = db.execute("SELECT * FROM research_runs WHERE id=%s",(run_id,)).fetchone()
        if row:
            row["manifest"] = db.execute("""
                SELECT m.path,m.dataset_id,m.checksum,m.row_count,m.start_ts,m.end_ts
                FROM research_data_pins p JOIN market_data_manifest m USING(path)
                WHERE p.run_id=%s ORDER BY m.path
            """,(run_id,)).fetchall()
    if not row:
        raise HTTPException(404,"研究任务不存在")
    return row


@router.post("/runs/{run_id}/stop")
def stop(run_id: UUID):
    with connect() as db:
        row = db.execute("""
            UPDATE research_runs SET stop_requested=true WHERE id=%s AND status IN ('QUEUED','RUNNING')
            RETURNING id,status,stop_requested
        """,(run_id,)).fetchone()
    return row or run_detail(run_id)


@router.get("/events")
def events(run_id: UUID, asset: str | None=None, direction: Literal["LONG","SHORT"] | None=None,
           state: Literal["CONFIRMED","REJECTED"] | None=None, episode: str | None=None,
           start_ms: int | None=None, end_ms: int | None=None,
           limit: int=Query(50,ge=1,le=100), offset: int=Query(0,ge=0,le=100000)):
    with connect() as db:
        return db.execute("""
            SELECT e.id,e.instrument_id,i.symbol,e.variant,e.direction,e.state,e.detected_ms,e.episode_id,
                   e.detail->'reasons' AS reasons
            FROM research_events e JOIN instruments i USING(instrument_id)
            WHERE e.run_id=%s AND (%s::text IS NULL OR e.instrument_id=%s)
              AND (%s::text IS NULL OR e.direction=%s) AND (%s::text IS NULL OR e.state=%s)
              AND (%s::text IS NULL OR e.episode_id=%s)
              AND (%s::bigint IS NULL OR e.detected_ms>=%s) AND (%s::bigint IS NULL OR e.detected_ms<%s)
            ORDER BY e.detected_ms DESC,e.id LIMIT %s OFFSET %s
        """,(run_id,asset,asset,direction,direction,state,state,episode,episode,start_ms,start_ms,end_ms,end_ms,limit,offset)).fetchall()


def event_row(db,key):
    row = db.execute("SELECT * FROM research_events WHERE id=%s",(key,)).fetchone()
    if not row:
        raise HTTPException(404,"事件不存在")
    return row


@router.get("/events/{key}")
def detail(key: str):
    with connect() as db:
        event = event_row(db,key)
        labels = db.execute("SELECT * FROM research_labels WHERE event_id=%s ORDER BY horizon_s",(key,)).fetchall()
        transitions = db.execute("SELECT * FROM research_event_transitions WHERE event_id=%s ORDER BY sequence",(key,)).fetchall()
    return {**event,"labels":labels,"transitions":transitions}


@router.get("/events/{key}/replay")
def replay(key: str, before: int=Query(10,ge=0,le=30), after: int=Query(10,ge=0,le=30)):
    with connect() as db:
        event = event_row(db,key)
        stamp_ms = event["detected_ms"]
        start,end = stamp_ms-before*1000,stamp_ms+after*1000
        ids = [event["instrument_id"]]
        if start==end:
            return {"rows":[],"truncated":False}
        try:
            archive = ArchivedFeed(db,event["run_id"],DATA_DIR)
            run = db.execute("SELECT input_sequence FROM research_runs WHERE id=%s",(event["run_id"],)).fetchone()
            sequence = run["input_sequence"]
            rows = archive.merge(feed_rows(db,ids,start,end,sequence),ids,start,end,sequence)
        except (OSError, ValueError) as exc:
            raise HTTPException(409,"原始行情无法完整读取，请缩短窗口或检查归档") from exc
    return {"rows":rows[:2000],"truncated":len(rows)>2000}


@router.get("/forward-returns")
def aggregates(run_id: UUID, diagnostic: bool = False):
    with connect() as db:
        task = db.execute("SELECT status FROM research_runs WHERE id=%s",(run_id,)).fetchone()
        if not task or (not diagnostic and task["status"] in ("FAILED","INSUFFICIENT_DATA","STOPPED")):
            return []
        return db.execute("""
            SELECT e.variant,l.horizon_s,count(*) AS attempts,count(DISTINCT e.episode_id) AS episodes,
                count(*) FILTER(WHERE l.status='COMPLETE') AS complete,
                count(*) FILTER(WHERE l.status='MISSING') AS missing,
                count(*) FILTER(WHERE l.status='CENSORED') AS censored,
                count(*) FILTER(WHERE l.status='PENDING') AS pending,
                count(*) FILTER(WHERE l.detail->'execution'->>'status'='insufficient_depth') AS depth_failures,
                count(*) FILTER(WHERE l.detail->'execution'->>'status'='filled') AS executable,
                avg((l.detail->>'return_log')::numeric)::float8 AS mean_log,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY (l.detail->>'return_log')::float8) AS median_log,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY (l.detail->>'return_log')::float8) AS p25_log,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY (l.detail->>'return_log')::float8) AS p75_log,
                avg((l.detail->>'mfe')::numeric)::float8 AS mean_mfe,
                avg((l.detail->>'mae')::numeric)::float8 AS mean_mae,
                avg((l.detail->'execution'->>'net_return')::numeric)::float8 AS net_return,
                avg((l.detail->'execution'->>'gross_return')::numeric)::float8 AS matched_gross_return,
                (avg((l.detail->'execution'->>'net_return')::numeric)
                    FILTER (WHERE l.detail->'execution' ? 'gross_return'))::float8 AS matched_net_return,
                sum((l.detail->'execution'->>'fees_usdc')::numeric) AS fees_usdc,
                sum((l.detail->'execution'->>'spread_usdc')::numeric) AS spread_usdc,
                sum((l.detail->'execution'->>'slippage_usdc')::numeric) AS slippage_usdc
            FROM research_labels l JOIN research_events e ON e.id=l.event_id
            WHERE e.run_id=%s GROUP BY e.variant,l.horizon_s ORDER BY e.variant,l.horizon_s
        """,(run_id,)).fetchall()


@router.get("/quality/retention")
def retention_preview(hot_days: int=Query(7,ge=1,le=365), archive_days: int=Query(90,ge=1,le=3650)):
    with connect() as db:
        rows = db.execute("""
            SELECT dataset_id,partition_date,market_type,row_count,path,
                   EXISTS (SELECT 1 FROM research_data_pins p WHERE p.path=m.path) AS pinned
            FROM market_data_manifest m WHERE dataset_id LIKE 'research.%%'
            AND partition_date < current_date-%s ORDER BY partition_date,path LIMIT 200
        """,(hot_days,)).fetchall()
    cutoff = now().date()-timedelta(days=archive_days)
    for row in rows:
        row["archive_purge_candidate"] = not row["pinned"] and row["partition_date"]<cutoff
    return {"dry_run":True,"hot_days":hot_days,"archive_days":archive_days,"partitions":rows}


@router.get("/models")
@router.get("/capacity")
def deferred():
    return {"status":"unavailable"}


@router.websocket("/ws")
async def updates(socket: WebSocket):
    from app.api.main import ORIGINS

    origin = socket.headers.get("origin")
    if origin and origin not in ORIGINS:
        await socket.close(code=1008)
        return
    await socket.accept()
    previous = None
    try:
        while True:
            revision = await asyncio.to_thread(latest_update)
            if revision != previous:
                await asyncio.wait_for(socket.send_json({"type":"research_update","revision":revision}),5)
                previous = revision
            try:
                message = await asyncio.wait_for(socket.receive(),3)
                if message["type"]=="websocket.disconnect":
                    return
            except TimeoutError:
                pass
    except (WebSocketDisconnect,TimeoutError):
        return


def latest_update():
    with connect() as db:
        row = db.execute("SELECT max(updated_at) AS stamp,count(*) AS runs FROM research_runs").fetchone()
        heartbeat = db.execute("SELECT state->>'connected' AS connected FROM worker_leases WHERE component='research_feed'").fetchone()
    return [str(row["stamp"]),row["runs"],heartbeat["connected"] if heartbeat else None]
