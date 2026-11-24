export const kinds = { PERP: '原生永续', SPOT: '现货', HIP3: 'HIP-3', OUTCOME: 'Outcomes' }
export const selectorNames = {
  volume: '24h 成交额', market_cap: '流通市值', gainers: '24h 涨幅',
  losers: '24h 跌幅', volatility: '30 分钟波动率', volume_spike: '5 分钟成交量异动',
}
export function numeric(value: number | string | null | undefined, maxDigits = 8) {
  return value == null ? 'N/A' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: maxDigits })
}
export function dateTime(value: string | null | undefined, utc = false) {
  if (!value) return 'N/A'
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'short', timeStyle: 'medium', timeZone: utc ? 'UTC' : undefined,
  }).format(new Date(value))
}
export function percent(value: number | null) {
  return value == null ? 'N/A' : `${numeric(value * 100, 3)}%`
}

