from datetime import timedelta

from psycopg.types.json import Jsonb

from app.market_data.repository import MARKETS
from app.selectors.ranking import Rules, select

ACTIVE = "NOT blocked AND (manual OR pinned OR automatic)"


def members(db, pool_id):
    return list(db.execute("""
        SELECT m.*, i.symbol, i.status FROM watchlist_members m JOIN instruments i USING (instrument_id)
        WHERE watchlist_id=%s AND (manual OR pinned OR blocked OR automatic)
        ORDER BY pinned DESC, symbol, instrument_id
    """, (pool_id,)))


def preview(db, pool, as_of):
    config = Rules.model_validate(pool["config"])
    previous = members(db, pool["id"])
    market = list(db.execute(MARKETS, {"as_of": as_of}))
    history = {}
    if set(config.selectors) & {"volatility", "volume_spike"}:
        bars = db.execute("""
            SELECT * FROM candles WHERE interval='1m' AND is_closed AND open_ts >= %s
            AND close_ts < %s AND received_ts <= %s ORDER BY instrument_id,open_ts
        """, (as_of - timedelta(minutes=32), as_of, as_of))
        for row in bars:
            history.setdefault(row["instrument_id"], []).append(row)
        history = {iid: rows[-31:] for iid, rows in history.items()}
    blocked = {row["instrument_id"] for row in previous if row["blocked"]}
    ranked, warnings = select(market, history, config, as_of, blocked)
    return project(pool, config, previous, ranked, market, as_of), warnings


def project(pool, rules, previous, ranked, market, as_of):
    if pool["type"] == "MANUAL":
        return []
    active = {row["instrument_id"] for row in market if row["status"] == "ACTIVE"}
    old = {row["instrument_id"]: row for row in previous}
    reserved = {iid for iid, row in old.items() if iid in active and not row["blocked"]
                and (row["manual"] or row["pinned"])}
    held = {iid for iid, row in old.items() if iid in active and not row["blocked"] and row["automatic"]
            and as_of - row["entered_at"] < timedelta(seconds=rules.min_hold_seconds)}
    by_id = {row["instrument_id"]: row for row in ranked}
    candidates = []
    for iid in held - by_id.keys():
        candidates.append(dict(instrument_id=iid, rank=None, score=None, reason="min_hold",
                               data_quality="unavailable", data_as_of=None, as_of=as_of, sources={}))
    for row in ranked:
        iid = row["instrument_id"]
        ranks = row["sources"].values()
        qualifies = (all if rules.combine == "INTERSECTION" else any)(rank <= rules.top_n for rank in ranks)
        if qualifies or old.get(iid, {}).get("automatic"):
            candidates.append(row)
    ordered = sorted(candidates, key=lambda row: (row["instrument_id"] not in held, not old.get(row["instrument_id"], {}).get("automatic"),
                                                   row["rank"] or 0, row["instrument_id"]))
    available = max(0, rules.capacity - len(reserved))
    automatic_limit = min(available, max(rules.top_n, len(held - reserved)))
    selected = []
    used = 0
    for row in ordered:
        iid = row["instrument_id"]
        if iid not in reserved:
            if used >= automatic_limit:
                continue
            used += 1
        selected.append(row)
    return selected


def refresh(db, pool, as_of):
    results, warnings = preview(db, pool, as_of)
    prior = {row["instrument_id"]: row for row in members(db, pool["id"]) if row["automatic"]}
    selected = {row["instrument_id"] for row in results}
    # Keep the previous membership on missing prerequisite data.
    if pool["type"] != "MANUAL" and not results and warnings:
        warnings.append({"reason": "membership_preserved"})
        results = [dict(instrument_id=iid, rank=None, score=None, reason="stale_selection",
                        data_quality="unavailable", data_as_of=None, sources={})
                   for iid, row in prior.items() if row["status"] == "ACTIVE" and not row["blocked"]]
        selected = {row["instrument_id"] for row in results}
    run = db.execute("""
        INSERT INTO selection_runs(watchlist_id,as_of,version,config,warnings)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (pool["id"], as_of, pool["version"], Jsonb(pool["config"]), Jsonb(warnings))).fetchone()["id"]
    for row in results:
        iid = row["instrument_id"]
        db.execute("""
            INSERT INTO watchlist_members(watchlist_id,instrument_id,automatic) VALUES (%s,%s,true)
            ON CONFLICT (watchlist_id,instrument_id) DO UPDATE SET automatic=true,updated_at=now(),
            entered_at=CASE WHEN watchlist_members.automatic THEN watchlist_members.entered_at ELSE now() END
        """, (pool["id"], iid))
        log_result(db, run, row, "KEPT" if iid in prior else "ENTERED")
    for iid in prior.keys() - selected:
        db.execute("UPDATE watchlist_members SET automatic=false,updated_at=now() WHERE watchlist_id=%s AND instrument_id=%s",
                   (pool["id"], iid))
        log_result(db, run, dict(instrument_id=iid, rank=None, score=None, reason="outside_selection",
                                 data_quality="unavailable", data_as_of=None, sources={}), "EXITED")
    db.execute("UPDATE watchlists SET last_refresh=%s WHERE id=%s", (as_of, pool["id"]))
    return {"id": run, "results": results, "warnings": warnings}


def log_result(db, run, row, action):
    db.execute("INSERT INTO selection_results VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (run, row["instrument_id"], row["rank"], row["score"], row["reason"], action,
                row["data_quality"], row["data_as_of"], Jsonb(row["sources"])))

