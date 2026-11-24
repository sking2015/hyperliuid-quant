import csv
from io import StringIO
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.backtest.service import BacktestDraft, BacktestSelection, preflight, queue, queue_auto
from app.storage.db import connect
from app.strategies.registry import REGISTRY

router = APIRouter(prefix="/api/v1")


@router.get("/strategies")
def strategies():
    return [definition.metadata() for definition in REGISTRY.values()]


@router.get("/strategy-instances")
def instances(limit: int = Query(30, ge=1, le=100)):
    with connect() as db:
        return db.execute("""
            SELECT id, configuration->>'strategy_id' AS strategy_id,
                configuration->>'strategy_version' AS strategy_version,
                configuration->'parameters' AS parameters, status, progress,
                started_at,finished_at, error,
                report->'strategy_state'->>'status' AS runtime_status
            FROM backtest_runs ORDER BY created_at DESC,id DESC LIMIT %s
        """, (limit,)).fetchall()


@router.post("/backtests/preflight")
def inspect_data(draft: BacktestDraft):
    return preflight(draft)


@router.post("/backtests", status_code=202)
def create_run(draft: BacktestDraft):
    return queue(draft)


@router.post("/backtests/auto", status_code=202)
def create_auto_run(selection: BacktestSelection):
    return queue_auto(selection)


@router.get("/backtests")
def runs(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    with connect() as db:
        return db.execute("""
            SELECT id,status,progress,configuration,created_at,started_at,finished_at,error
            FROM backtest_runs ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s
        """, (limit, offset)).fetchall()


def run(db, run_id, lock=False):
    row = db.execute("SELECT * FROM backtest_runs WHERE id=%s" + (" FOR UPDATE" if lock else ""),
                     (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到回测任务")
    return row


@router.get("/backtests/{run_id}")
def run_status(run_id: UUID):
    with connect() as db:
        row = run(db, run_id)
    row.pop("report")
    return row


@router.post("/backtests/{run_id}/cancel")
def cancel(run_id: UUID):
    with connect() as db:
        row = run(db, run_id, lock=True)
        if row["status"] in {"QUEUED", "RUNNING"}:
            row = db.execute("""
                UPDATE backtest_runs SET cancel_requested=true,
                  status=CASE WHEN status='QUEUED' THEN 'CANCELLED' ELSE status END,
                  finished_at=CASE WHEN status='QUEUED' THEN now() ELSE finished_at END
                WHERE id=%s RETURNING id,status,cancel_requested
            """, (run_id,)).fetchone()
        return {key: row[key] for key in ("id", "status", "cancel_requested")}


@router.get("/backtests/{run_id}/report")
def report(run_id: UUID):
    with connect() as db:
        row = run(db, run_id)
    if row["status"] != "SUCCEEDED":
        raise HTTPException(409, "回测报告尚不可用")
    return row["report"]


@router.get("/backtests/{run_id}/ledger")
def ledger(run_id: UUID):
    with connect() as db:
        run(db, run_id)
        return [row["entry"] for row in db.execute(
            "SELECT entry FROM backtest_ledger WHERE run_id=%s ORDER BY entry->>'time',event_key", (run_id,))]


@router.get("/backtests/{run_id}/trades.csv")
def trades_csv(run_id: UUID):
    trades = report(run_id)["trades"]
    output = StringIO()
    fields = ["instrument_id", "side", "entry_time", "exit_time", "quantity", "entry", "exit",
              "stop", "target", "trailing_stop", "reason", "entry_reason", "gross_pnl", "fees", "funding", "net_pnl"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(trades)
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}-trades.csv"'})

