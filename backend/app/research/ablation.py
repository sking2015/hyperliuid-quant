from collections import defaultdict
from math import floor
from random import Random


def control_key(row, config):
    volatility, spread = row["volatility"], row["spread"]
    if volatility is None or spread is None:
        return None
    return (row["instrument_id"], row["direction"], row["detected_ms"]//(config.match_window_s*1000),
            floor(volatility*10000/config.match_vol_bps), floor(spread/config.match_spread_bps))


def paired_summary(rows, config, variant, horizon):
    found = [row for row in rows if row["horizon_s"]==horizon and row["variant"]==variant]
    episodes = {row["episode_id"] for row in found}
    pools = defaultdict(list)
    for row in rows:
        key = control_key(row,config)
        if key is not None and row["horizon_s"]==horizon and row["variant"]=="A" and row["episode_id"] not in episodes:
            pools[key].append(row)
    effective = max(config.match_window_s,max(config.horizons_s))*1000
    blocks = defaultdict(list)
    pairs = 0
    for row in found:
        possible = pools.get(control_key(row,config),[])
        if not possible:
            continue
        rival = min(possible,key=lambda other:(abs(other["detected_ms"]-row["detected_ms"]),
                                             other["detected_ms"],other["episode_id"]))
        possible.remove(rival)
        blocks[row["detected_ms"]//effective].append(row["return_log"]-rival["return_log"])
        pairs += 1
    chunks = [(sum(values),len(values)) for values in blocks.values()]
    result = dict(variant=variant,horizon_s=horizon,treated=len(found),pairs=pairs,blocks=len(chunks),
                  mean_difference=sum(total for total,_ in chunks)/pairs if pairs else None,
                  ci95=None, status="INSUFFICIENT_MATCHES")
    if len(chunks)>=config.bootstrap_min_blocks:
        random = Random(config.bootstrap_seed)
        samples = []
        for _ in range(config.bootstrap_iterations):
            draw = random.choices(chunks,k=len(chunks))
            samples.append(sum(total for total,_ in draw)/sum(count for _,count in draw))
        samples.sort()
        result.update(ci95=[samples[int(.025*(len(samples)-1))],samples[int(.975*(len(samples)-1))]],
                      status="EXPLORATORY" if pairs<100 else "ESTIMATED")
    return result


def ablation_report(db, run_id, config):
    rows = db.execute("""
        SELECT e.id,e.instrument_id,e.direction,e.variant,e.episode_id,e.detected_ms,l.horizon_s,
               (l.detail->>'return_log')::float8 AS return_log,
               (e.detail->'features'->'robust_sigma_1800'->>'value')::float8 AS volatility,
               (e.detail->'features'->'spread_median_60'->>'value')::float8 AS spread,
               abs((e.detail->'features'->'tfi_15'->>'value')::float8) AS tfi,
               (e.detail->'features'->'rvol_30'->>'value')::float8 AS rvol,
               (e.detail->'features'->('persistence_'||lower(e.direction))->>'value')::float8 AS persistence
        FROM research_events e JOIN research_labels l ON l.event_id=e.id
        WHERE e.run_id=%s AND l.status='COMPLETE' AND l.detail ? 'return_log'
        ORDER BY e.detected_ms,e.instrument_id,e.direction,e.variant,e.episode_id,l.horizon_s LIMIT 20001
    """,(run_id,)).fetchall()
    if len(rows)>20000:
        return {"status":"LIMIT_EXCEEDED","comparisons":[]}
    groups = defaultdict(list)
    bins = defaultdict(list)
    edges = {"tfi":(.3,.5,.7), "rvol":(1.5,2.,3.,5.), "persistence":(.6,.75,.9), "spread":(3.,5.,8.,15.)}
    for row in rows:
        key = (row["variant"],row["horizon_s"])
        groups[key].append(row)
        for feature, thresholds in edges.items():
            value = row[feature]
            bucket = "unknown" if value is None else str(sum(value >= edge for edge in thresholds))
            bins[(*key,feature,bucket)].append(row["return_log"])
    return {"status":"AVAILABLE","comparisons":[paired_summary(rows,config,variant,horizon)
            for variant in config.variants if variant!="A" for horizon in config.horizons_s],
            "absolute":[absolute_summary(key,group,config) for key,group in sorted(groups.items())],
            "bins":[dict(variant=key[0],horizon_s=key[1],feature=key[2],bucket=key[3],
                         samples=len(sample),mean_log=sum(sample)/len(sample))
                    for key,sample in sorted(bins.items())],"bin_edges":edges}


def absolute_summary(key, rows, config):
    blocks = defaultdict(list)
    width = max(key[1],config.cooldown_s,config.match_window_s)*1000
    for row in rows:
        blocks[row["detected_ms"]//width].append(row["return_log"])
    chunks = [(sum(values),len(values)) for _,values in sorted(blocks.items())]
    ci = None
    if len(chunks) >= config.bootstrap_min_blocks:
        rng = Random(config.bootstrap_seed)
        estimates = []
        for _ in range(config.bootstrap_iterations):
            draw = rng.choices(chunks,k=len(chunks))
            estimates.append(sum(value for value,_ in draw)/sum(n for _,n in draw))
        estimates.sort()
        ci = [estimates[int(p*(len(estimates)-1))] for p in (.025,.975)]
    return dict(variant=key[0],horizon_s=key[1],samples=len(rows),blocks=len(chunks),
                ci95=ci,status="ESTIMATED" if ci is not None else "INSUFFICIENT_BLOCKS")

