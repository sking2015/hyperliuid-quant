export type Kind = 'PERP' | 'SPOT' | 'HIP3' | 'OUTCOME'
export type Selector = 'volume' | 'market_cap' | 'gainers' | 'losers' | 'volatility' | 'volume_spike'
export interface Rules {
  selectors: Selector[]; combine: 'UNION' | 'INTERSECTION'; top_n: number; exit_rank: number
  capacity: number; refresh_seconds: number; min_hold_seconds: number; min_volume: number | string
  max_spread_bps: number | string | null; market_types: Kind[]; max_age_seconds: number
}
export interface Draft { name: string; type: 'MANUAL' | 'DYNAMIC' | 'MIXED'; enabled: boolean; config: Rules }
export interface Pool extends Draft { id: string; version: number }
export interface Member {
  instrument_id: string; symbol: string; status: string; manual: boolean
  pinned: boolean; blocked: boolean; automatic: boolean
}
export interface MarketIdentity { instrument_id: string; symbol: string }
export interface Market extends MarketIdentity {
  market_type: Kind; quote: string | null
  mark: number | null; volume_24h: number | null; change_24h: number | null
  market_cap: number | null; open_interest: number | null; funding: number | null
  bid: number | null; ask: number | null; spread_bps: number | null
  stale: boolean; as_of: string | null; status: string; paper_supported: boolean
}
export interface Bar { open_ts: string; o: number; h: number; l: number; c: number; v: number; is_closed: boolean }
export interface Result {
  instrument_id: string; symbol?: string; reason: string; score: number | null; rank: number | null
  data_quality: string; action?: string; sources: Record<string, number>
}
export interface Warning { selector?: Selector; unavailable?: number; eligible?: number; reason?: string }
export interface Run { id: number; as_of: string; version: number; results: Result[]; warnings: Warning[] }
export interface RequestMetrics { requests: number; errors: number; error_rate: number | null; latency_ms: number | null }
export interface Status {
  api_metrics?: RequestMetrics
  worker_stale: boolean; observed: number
  counts: { market_type: Kind; count: number }[]
  coverage: { candle_jobs: number; jobs_due: number; last_candle: string | null; bootstrap_remaining?: number; bootstrap_due?: number; recurring_due?: number }
  gaps: { data_type: string; count: number; recent?: number }[]
  integrity?: { stale_observed_1m: number }
  worker: { heartbeat: string; state: {
    ws_connected?: boolean; subscriptions?: number; subscription_references?: number
    subscription_overflow?: number; ws_reconnects?: number; snapshot_at?: string
    snapshot_error?: string; history_error?: string; funding_error?: string; market_cap_error?: string; ws_error?: string; disk_free_bytes?: number
    database_bytes?: number | null; data_file_bytes?: number | null
    disk_alert?: string; collection_paused?: boolean; quota_remaining_bytes?: number
    host_probe_free_bytes?: number | null; disk_error?: string; ws_latency_ms?: number; ws_data_at?: string
    upstream_api?: RequestMetrics
  } } | null
}

