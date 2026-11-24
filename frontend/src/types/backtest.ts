export type Parameters = Record<string, string | number>
export interface StrategyDefinition {
  id: string; name: string; version: string; code_hash: string; defaults: Parameters
  parameter_schema: { properties: Record<string, { enum?: string[]; minimum?: number; maximum?: number }> }
}
export interface BacktestDraft {
  strategy_id: string; parameters: Parameters; instrument_ids: string[]; watchlist_id: string | null
  start: string; end: string; fee_rate: number; slippage: number; require_funding: boolean
}
export interface Run {
  id: string; status: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  progress: number; created_at: string; error: string | null
  configuration: BacktestDraft & { markets: { instrument_id: string; symbol: string }[]; strategy_version: string }
}
export interface Coverage {
  instrument_id: string; expected: number; available: number; missing: number; missing_funding_hours: number
  invalid?: number; missing_warmup?: number; missing_requested?: number; first_missing?: string | null
  symbol?: string; local_start?: string | null; local_end?: string | null
}
export interface Manifest {
  ready: boolean; funding_partial: boolean; candle_sources: string[]; funding_sources: string[]
  coverage: Coverage[]; warmup_bars: number; sha256?: string
  interval?: string; require_funding?: boolean; requested_start?: string; requested_end?: string
  suggested_range?: { start: string; end: string } | null
}
export interface Trade {
  instrument_id: string; side: 'LONG' | 'SHORT'; entry_time: string; exit_time: string
  entry: string; exit: string; stop: string; target: string | null; trailing_stop?: string | null; quantity: string; fees: string
  funding: string; net_pnl: string; reason: string
}
export interface Report {
  metrics: Record<string, string | number | null>
  manifest: Manifest
  trades: Trade[]
  signal_research?: { minutes: number; samples: number; return_rate: string | null; mfe: string | null; mae: string | null }[]
  equity_curve: { time: string; equity: string }[]
  open_positions: { instrument_id: string; side: string; entry: string; mark: string; unrealized: string }[]
}
export const minutes: Record<string, number> = { '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240, '1d': 1440 }
export const reasons: Record<string, string> = { stop_loss: '止损', take_profit: '止盈', trailing_stop: '移动止损', reverse_signal: '反向平仓' }



export interface AutoBacktest {
  id: string; start: string; end: string; interval: string
  coverage: { instrument_id: string; symbol: string; bars: number; warmup_bars: number }[]
}
