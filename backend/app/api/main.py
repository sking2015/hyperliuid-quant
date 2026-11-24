import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.config import MINUTES
from app.market_data.import_jobs import input_path
from app.market_data.repository import MARKETS, now
from app.selectors.ranking import Rules
from app.storage.db import connect, migrate
from app.watchlist.service import ACTIVE, log_result, members, preview, refresh

API = "/api/v1"
ORIGINS = {origin.strip() for origin in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"
).split(",") if origin.strip()}
MarketType = Literal["PERP", "SPOT", "HIP3", "OUTCOME"]


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(migrate)
    yield


app = FastAPI(title="HL Quant Lab", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def request_id(request, call_next):
    request.state.request_id = str(uuid4())
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in ORIGINS:
        response = await error(request, HTTPException(403, "请求来源未授权"))
    else:
        response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(HTTPException)
@app.exception_handler(RequestValidationError)
@app.exception_handler(OperationalError)
async def error(request: Request, exc):
    if isinstance(exc, HTTPException):
        status, message, details = exc.status_code, exc.detail, None
    elif isinstance(exc, RequestValidationError):
        status, message, details = 422, "请求参数无效", jsonable_encoder(exc.errors())
    else:
        status, message, details = 503, "数据库暂不可用", None
    return JSONResponse({"code": str(status), "message": message, "details": details,
                         "request_id": request.state.request_id}, status_code=status)


def system_status():
    with connect() as db:
        worker = db.execute("SELECT * FROM worker_leases WHERE component='market'").fetchone()
        counts = db.execute("SELECT market_type,count(*) FROM instruments WHERE status='ACTIVE' GROUP BY market_type").fetchall()
        coverage = db.execute("""
            SELECT count(*) AS candle_jobs, count(*) FILTER (WHERE next_due <= now()) AS jobs_due,
                   max(progress_ts) AS last_candle FROM history_jobs
        """).fetchone()
        subscriptions = db.execute(f"""
            SELECT count(DISTINCT instrument_id) AS observed FROM watchlist_members
            JOIN watchlists ON watchlists.id=watchlist_members.watchlist_id
            JOIN instruments USING (instrument_id)
            WHERE watchlists.enabled AND deleted_at IS NULL AND instruments.status='ACTIVE' AND {ACTIVE}
        """).fetchone()
        gaps = db.execute("SELECT data_type,count(*) FROM data_gaps GROUP BY data_type").fetchall()
    stamp = now()
    return dict(paper_only=True, now=stamp, worker=worker, counts=counts, coverage=coverage,
                observed=subscriptions["observed"], gaps=gaps,
                worker_stale=worker is None or worker["heartbeat"] < stamp - timedelta(seconds=20))


@app.get(API + "/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "paper_only": True}


@app.get(API + "/system/status")
def status():
    return system_status()


@app.get(API + "/markets")
def markets(q: str = Query("", max_length=100), market_type: MarketType | None = None,
            sort: Literal["symbol", "volume_24h", "change_24h", "mark", "market_cap"] = "volume_24h",
            direction: Literal["asc", "desc"] = "desc", offset: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200)):
    clauses = " WHERE (symbol ILIKE %(q)s OR asset_key ILIKE %(q)s) AND (%(kind)s::text IS NULL OR market_type=%(kind)s)"
    params = dict(q=f"%{q}%", kind=market_type, as_of=now(), limit=limit, offset=offset)
    with connect() as db:
        total = db.execute("SELECT count(*) FROM instruments" + clauses, params).fetchone()["count"]
        rows = db.execute(MARKETS + clauses + f" ORDER BY {sort} {direction} NULLS LAST, instrument_id LIMIT %(limit)s OFFSET %(offset)s",
                          params).fetchall()
    for row in rows:
        row["stale"] = row["as_of"] is None or row["as_of"] < params["as_of"] - timedelta(minutes=3)
        row["open_interest_unit"] = "base" if row["open_interest"] is not None else None
        row["spread_bps"] = ((row["ask"] - row["bid"]) * 20000 / (row["ask"] + row["bid"])
                             if row["bid"] and row["ask"] and row["ask"] >= row["bid"] else None)
    return {"items": rows, "total": total, "as_of": params["as_of"]}


def require_instrument(db, instrument_id):
    if not db.execute("SELECT 1 FROM instruments WHERE instrument_id=%s", (instrument_id,)).fetchone():
        raise HTTPException(404, "找不到市场")


@app.get(API + "/markets/{instrument_id}/candles")
def candles(instrument_id: str, interval: str = "1m", start: datetime | None = None,
            end: datetime | None = None, limit: int = Query(200, ge=1, le=5000)):
    if interval not in MINUTES or any(stamp is not None and stamp.utcoffset() is None for stamp in (start, end)):
        raise HTTPException(422, "请使用已支持的周期及带时区的时间")
    if start and end and start >= end:
        raise HTTPException(422, "结束时间必须晚于开始时间")
    with connect() as db:
        require_instrument(db, instrument_id)
        rows = db.execute("""
            SELECT * FROM candles WHERE instrument_id=%s AND interval=%s
            AND (%s::timestamptz IS NULL OR open_ts >= %s) AND (%s::timestamptz IS NULL OR open_ts < %s)
            ORDER BY open_ts DESC LIMIT %s
        """, (instrument_id, interval, start, start, end, end, limit)).fetchall()
    return list(reversed(rows))


@app.get(API + "/markets/{instrument_id}/coverage")
def coverage(instrument_id: str, limit: int = Query(100, ge=1, le=500)):
    with connect() as db:
        require_instrument(db, instrument_id)
        ranges = db.execute("""
            SELECT interval,min(open_ts) AS start,max(close_ts) AS end,count(*) AS bars
            FROM candles WHERE instrument_id=%s AND is_closed GROUP BY interval ORDER BY interval
        """, (instrument_id,)).fetchall()
        gaps = db.execute("SELECT * FROM data_gaps WHERE instrument_id=%s ORDER BY start_ts DESC LIMIT %s",
                          (instrument_id, limit)).fetchall()
    return {"ranges": ranges, "gaps": gaps}


class ImportDraft(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    instrument_id: str = Field(min_length=1, max_length=100)
    interval: Literal["1m", "3m", "5m", "15m", "1h", "4h", "1d"]
    source: str = Field(min_length=1, max_length=100, pattern=r"\S")


@app.post(API + "/data/import-jobs", status_code=202)
def queue_import(body: ImportDraft, idempotency_key: str | None = Header(None, max_length=128)):
    with connect() as db:
        if idempotency_key:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 6022030))", (idempotency_key,))
            previous = db.execute("SELECT * FROM data_import_jobs WHERE idempotency_key=%s", (idempotency_key,)).fetchone()
            if previous:
                if any(previous[key] != value for key, value in body.model_dump().items()):
                    raise HTTPException(409, "幂等键已用于其他导入请求")
                return previous
        require_instrument(db, body.instrument_id)
        try:
            input_path(body.filename)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return db.execute("""
            INSERT INTO data_import_jobs(id, instrument_id, interval, filename, source, idempotency_key)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uuid4(), body.instrument_id, body.interval, body.filename, body.source, idempotency_key)).fetchone()


@app.get(API + "/data/import-jobs/{job_id}")
def import_status(job_id: UUID):
    with connect() as db:
        job = db.execute("SELECT * FROM data_import_jobs WHERE id=%s", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(404, "找不到导入任务")
        return job


class Pool(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    type: Literal["MANUAL", "DYNAMIC", "MIXED"] = "MANUAL"
    config: Rules = Field(default_factory=Rules)
    enabled: bool = True


class Member(BaseModel):
    manual: bool = True
    pinned: bool | None = None
    blocked: bool | None = None


def get_pool(db, pool_id, lock=False):
    row = db.execute("SELECT * FROM watchlists WHERE id=%s AND deleted_at IS NULL" +
                     (" FOR UPDATE" if lock else ""), (pool_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到观察池")
    return row


@app.get(API + "/watchlists")
def list_pools():
    with connect() as db:
        return db.execute("SELECT * FROM watchlists WHERE deleted_at IS NULL ORDER BY name,id").fetchall()


@app.post(API + "/watchlists", status_code=201)
def create_pool(body: Pool):
    with connect() as db:
        return db.execute("""
            INSERT INTO watchlists(id,name,type,config,enabled) VALUES (%s,%s,%s,%s,%s) RETURNING *
        """, (uuid4(), body.name.strip(), body.type, Jsonb(body.config.model_dump(mode="json")), body.enabled)).fetchone()


@app.post(API + "/watchlists/preview")
def preview_draft(draft: Pool):
    with connect() as db:
        results, warnings = preview(db, {"id": uuid4(), "type": draft.type,
                                         "config": draft.config.model_dump(mode="json")}, now())
    return {"results": results, "warnings": warnings}


@app.get(API + "/watchlists/{pool_id}")
def pool_detail(pool_id: UUID):
    with connect() as db:
        pool = get_pool(db, pool_id)
        return pool | {"members": members(db, pool_id)}


@app.patch(API + "/watchlists/{pool_id}")
def edit_pool(pool_id: UUID, body: Pool):
    with connect() as db:
        pool = get_pool(db, pool_id, True)
        config = body.config.model_dump(mode="json")
        if db.execute(f"SELECT count(*) AS n FROM watchlist_members WHERE watchlist_id=%s AND {ACTIVE}",
                      (pool_id,)).fetchone()["n"] > body.config.capacity:
            raise HTTPException(409, "当前成员数超过新容量")
        new = db.execute("""
            UPDATE watchlists SET name=%s,type=%s,config=%s,enabled=%s,version=version+1,last_refresh=NULL
            WHERE id=%s RETURNING *
        """, (body.name.strip(), body.type, Jsonb(config), body.enabled, pool_id)).fetchone()
        if body.type == "MANUAL" and pool["type"] != "MANUAL":
            return new | refresh(db, new, now()) | {"id": pool_id}
        return new


@app.delete(API + "/watchlists/{pool_id}")
def delete_pool(pool_id: UUID):
    with connect() as db:
        get_pool(db, pool_id, True)
        db.execute("UPDATE watchlists SET enabled=false,deleted_at=now() WHERE id=%s", (pool_id,))
    return {"deleted": True}


def change_member(db, pool, instrument, body):
    pool_id = pool["id"]
    row = db.execute("""
        SELECT * FROM watchlist_members WHERE watchlist_id=%s AND instrument_id=%s
    """, (pool_id, instrument)).fetchone() or {}
    flags = {key: bool(row.get(key)) if value is None else value for key, value in body.model_dump().items()}
    before = not row.get("blocked") and any(row.get(k) for k in ("manual", "pinned", "automatic"))
    after = not flags["blocked"] and (flags["manual"] or flags["pinned"] or row.get("automatic"))
    if after and not before:
        count = db.execute(f"SELECT count(*) AS n FROM watchlist_members WHERE watchlist_id=%s AND {ACTIVE}",
                           (pool_id,)).fetchone()["n"]
        if count >= pool["config"]["capacity"]:
            raise HTTPException(409, "观察池已满")
    if all(bool(row.get(k)) == v for k, v in flags.items()):
        return {"changed": False}
    db.execute("""
        INSERT INTO watchlist_members(watchlist_id,instrument_id,manual,pinned,blocked) VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (watchlist_id,instrument_id) DO UPDATE SET manual=excluded.manual,
        pinned=excluded.pinned,blocked=excluded.blocked,updated_at=now()
    """, (pool_id, instrument, flags["manual"], flags["pinned"], flags["blocked"]))
    run = db.execute("""
        INSERT INTO selection_runs(watchlist_id,as_of,version,config,warnings)
        VALUES (%s,now(),%s,%s,'[]') RETURNING id
    """, (pool_id, pool["version"], Jsonb(pool["config"]))).fetchone()["id"]
    log_result(db, run, dict(instrument_id=instrument, rank=None, score=None,
                            reason="blacklist" if flags["blocked"] else "manual", data_quality="manual",
                            data_as_of=None, sources={}), "ENTERED" if after and not before else
               "EXITED" if before and not after else "UPDATED")
    return {"changed": True}


@app.post(API + "/watchlists/{pool_id}/members/{instrument_id}")
def add_member(pool_id: UUID, instrument_id: str, body: Member = Member()):
    with connect() as db:
        pool = get_pool(db, pool_id, True)
        require_instrument(db, instrument_id)
        return change_member(db, pool, instrument_id, body)


@app.delete(API + "/watchlists/{pool_id}/members/{instrument_id}")
def remove_member(pool_id: UUID, instrument_id: str):
    with connect() as db:
        pool = get_pool(db, pool_id, True)
        require_instrument(db, instrument_id)
        current = db.execute("SELECT blocked FROM watchlist_members WHERE watchlist_id=%s AND instrument_id=%s",
                             (pool_id, instrument_id)).fetchone()
        return change_member(db, pool, instrument_id, Member(manual=False, pinned=False,
                                                              blocked=bool(current and current["blocked"])))


@app.post(API + "/watchlists/{pool_id}/preview")
def preview_pool(pool_id: UUID, draft: Pool | None = None):
    with connect() as db:
        pool = get_pool(db, pool_id)
        if draft:
            pool |= {"type": draft.type, "config": draft.config.model_dump(mode="json")}
        results, warnings = preview(db, pool, now())
        return {"results": results, "warnings": warnings}


@app.post(API + "/watchlists/{pool_id}/refresh")
def refresh_pool(pool_id: UUID):
    with connect() as db:
        return refresh(db, get_pool(db, pool_id, True), now())


@app.get(API + "/watchlists/{pool_id}/history")
def history(pool_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50)):
    with connect() as db:
        get_pool(db, pool_id)
        runs = db.execute("""
            SELECT s.*, COALESCE(r.results,'[]') AS results FROM
            (SELECT * FROM selection_runs WHERE watchlist_id=%s ORDER BY id DESC LIMIT %s OFFSET %s) s
            LEFT JOIN LATERAL (
              SELECT jsonb_agg(to_jsonb(x) ORDER BY x.rank NULLS LAST,x.instrument_id) AS results
              FROM (SELECT selection_results.*,symbol FROM selection_results JOIN instruments USING (instrument_id)
                    WHERE run_id=s.id) x
            ) r ON true ORDER BY s.id DESC
        """, (pool_id, limit, offset)).fetchall()
    return runs


@app.websocket(API + "/ws/events")
async def events(socket: WebSocket):
    origin = socket.headers.get("origin")
    if origin and origin not in ORIGINS:
        await socket.close(code=1008)
        return
    await socket.accept()
    previous = None
    seq = 0
    try:
        while True:
            payload = await asyncio.to_thread(system_status)
            stamp = payload.pop("now")
            if payload != previous:
                seq += 1
                await asyncio.wait_for(socket.send_json(jsonable_encoder(
                    dict(type="system_status", ts=stamp, seq=seq, scope="system", payload=payload))), timeout=5)
                previous = payload
            try:
                if (await asyncio.wait_for(socket.receive(), timeout=2))["type"] == "websocket.disconnect":
                    break
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, OperationalError, asyncio.TimeoutError):
        return

