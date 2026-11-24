<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { CandlestickSeries, createChart, createSeriesMarkers, LineStyle, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { api } from '../api/client'
import { dateTime, numeric, percent } from '../format'
import type { Bar, Market, MarketIdentity } from '../types/models'

import { minutes, reasons, type Trade } from '../types/backtest'
const props = defineProps<{ instrument?: MarketIdentity; utc: boolean; trade?: Trade; tradeInterval?: string }>()
const emit = defineEmits<{ close: [] }>()
const visible = computed({
  get: () => Boolean(props.instrument),
  set: (value) => { if (!value) emit('close') },
})
const interval = ref('1m')
const snapshot = ref<Market>()
const snapshotError = ref('')
let chartVersion = 0
let snapshotVersion = 0
const chartTarget = ref<HTMLElement>(); const chartError = ref('')
const coverage = ref<{ gaps: unknown[] }>()
const lastBar = ref<Bar>()
let chart: IChartApi | undefined

async function openChart() {
  if (!props.instrument) return
  const revision = ++snapshotVersion
  const path = '/markets/' + encodeURIComponent(props.instrument.instrument_id)
  void draw()
  try {
    const market = await api<Market>(path)
    if (visible.value && revision === snapshotVersion) snapshot.value = market
  } catch (cause) {
    if (visible.value && revision === snapshotVersion) snapshotError.value = (cause as Error).message
  }
}

const coverageRange = ref<[Date, Date]>()
const coverageResult = ref<{ available: number; expected: number; missing: number; coverage: number | null }>()
const coverageError = ref('')

async function inspectCoverage() {
  if (!props.instrument || !coverageRange.value?.[0] || !coverageRange.value[1]) return
  const id = props.instrument.instrument_id
  const period = interval.value
  const [start, end] = coverageRange.value
  coverageResult.value = undefined
  coverageError.value = ''
  try {
    const query = new URLSearchParams({ interval: period, start: start.toISOString(), end: end.toISOString() })
    const data = await api<NonNullable<typeof coverageResult.value>>('/markets/' + encodeURIComponent(id) + '/coverage?' + query)
    if (props.instrument?.instrument_id === id && interval.value === period) coverageResult.value = data
  } catch (cause) { if (props.instrument?.instrument_id === id) coverageError.value = (cause as Error).message }
}

function tradeBars(path: string, fill: Trade, period: string) {
  const step = minutes[period] * 60000
  const first = Math.floor(Date.parse(fill.entry_time) / step) * step
  const last = Math.floor(Date.parse(fill.exit_time) / step) * step
  const query = (start: number, end: number) => api<Bar[]>(path + '/candles?' + new URLSearchParams({
    start: new Date(start).toISOString(), end: new Date(end).toISOString(), interval: period, limit: '5000',
  }))
  if (last - first < 4900 * step) return query(first - 12 * step, last + 13 * step)
  return Promise.all([query(first - 12 * step, first + 13 * step), query(last - 12 * step, last + 13 * step)])
    .then(([entry, exit]) => [...entry, ...exit])
}

async function draw() {
  if (!props.instrument || !visible.value) return
  const revision = ++chartVersion
  const path = '/markets/' + encodeURIComponent(props.instrument.instrument_id)
  chartError.value = ''
  lastBar.value = undefined
  coverage.value = undefined
  chart?.remove()
  chart = undefined
  try {
    const period = props.trade ? props.tradeInterval ?? '15m' : interval.value
    const [bars, result] = await Promise.all([
      props.trade ? tradeBars(path, props.trade, period) : api<Bar[]>(path + '/candles?interval=' + period),
      api<{ gaps: unknown[] }>(path + '/coverage')])
    await nextTick()
    if (revision !== chartVersion || !visible.value || !chartTarget.value) return
    coverage.value = result
    lastBar.value = bars.at(-1)
    if (!bars.length) { chartError.value = '该周期暂无已采集 K 线'; return }
    chart = createChart(chartTarget.value, { autoSize: true, timeScale: { timeVisible: true },
      localization: { timeFormatter: (t: number) => dateTime(new Date(t * 1000).toISOString(), props.utc) } })
    const styles = getComputedStyle(chartTarget.value)
    const up = styles.getPropertyValue('--up').trim()
    const down = styles.getPropertyValue('--down').trim()
    const series = chart.addSeries(CandlestickSeries, { upColor: up, downColor: down,
      borderVisible: false, wickUpColor: up, wickDownColor: down })
    series.setData(bars.map(b => ({
      time: Math.floor(Date.parse(b.open_ts) / 1000) as UTCTimestamp,
      open: Number(b.o), high: Number(b.h), low: Number(b.l), close: Number(b.c),
    })))
    if (props.trade) {
      const fill = props.trade
      const step = minutes[period] * 60
      const stamp = (value: string) => (Math.floor(Date.parse(value) / 1000 / step) * step) as UTCTimestamp
      const visibleTimes = new Set(bars.map(bar => Math.floor(Date.parse(bar.open_ts) / 1000)))
      createSeriesMarkers(series, ([
        { time: stamp(fill.entry_time), position: 'belowBar', color: up, shape: 'arrowUp', text: '开仓' },
        { time: stamp(fill.exit_time), position: 'aboveBar', color: down, shape: 'arrowDown', text: reasons[fill.reason] ?? '平仓' },
      ] as const).filter(marker => visibleTimes.has(marker.time)))
      if (fill.target != null) series.createPriceLine({ price: Number(fill.target), color: up, lineStyle: LineStyle.Dashed, title: '止盈' })
      if (fill.reason === 'trailing_stop' && fill.trailing_stop != null) {
        series.createPriceLine({ price: Number(fill.trailing_stop), color: down, lineStyle: LineStyle.Dashed, title: '移动止损触发线' })
      }
      series.createPriceLine({ price: Number(fill.stop), color: down, lineStyle: LineStyle.Dashed, title: '止损' })
    }
    chart.timeScale().fitContent()
  } catch (cause) {
    if (revision === chartVersion) chartError.value = (cause as Error).message
  }
}

function closeChart() {
  chartVersion++
  snapshotVersion++
  chart?.remove()
  chart = undefined
  chartError.value = ''
  snapshotError.value = ''
  snapshot.value = undefined
  lastBar.value = undefined
  coverage.value = undefined
  coverageResult.value = undefined
  coverageRange.value = undefined
  coverageError.value = ''
}

watch(visible, (isOpen) => { if (!isOpen) closeChart() })
watch(interval, () => { coverageResult.value = undefined })
watch([interval, () => props.utc], () => void draw())
onUnmounted(closeChart)
</script>

<template>
  <el-dialog v-model="visible" :title="instrument?.symbol" width="min(900px, 94vw)" @opened="openChart">
    <div v-if="instrument && !trade" class="toolbar">
      <el-select v-model="interval" aria-label="K 线周期" style="width:100px"><el-option v-for="i in ['1m','3m','5m','15m','30m','1h','4h','1d']" :key="i" :value="i" /></el-select>
      <span>资金费率 {{ percent(snapshot?.funding ?? null) }}</span><span>OI（基础资产）{{ numeric(snapshot?.open_interest) }}</span><span>价差 {{ numeric(snapshot?.spread_bps, 2) }} bps</span>
    </div>
    <p v-if="snapshot && !trade" class="muted">标记价 {{ numeric(snapshot.mark) }} {{ snapshot.quote }} · 24h 涨跌 {{ percent(snapshot.change_24h) }} · {{ snapshot.stale ? '行情过期或缺失' : dateTime(snapshot.as_of, utc) }}</p>
    <el-alert v-if="snapshotError" :title="snapshotError" type="warning" :closable="false" />
    <el-alert v-if="chartError" :title="chartError" type="info" :closable="false" />
    <div ref="chartTarget" class="chart" v-show="!chartError" />
    <p v-if="lastBar">{{ dateTime(lastBar.open_ts, utc) }} · {{ lastBar.is_closed ? '已闭合' : '未闭合' }} · 量 {{ numeric(lastBar.v) }}（基础资产）</p>
    <p v-if="coverage">近期记录的行情缺口 {{ coverage.gaps.length }} 条（最多显示 100 条）</p>
    <div v-if="!trade" class="toolbar">
      <el-date-picker v-model="coverageRange" type="datetimerange" start-placeholder="覆盖率起点" end-placeholder="覆盖率终点" />
      <el-button :disabled="!coverageRange" @click="inspectCoverage">查询历史覆盖率</el-button>
    </div>
    <el-alert v-if="coverageError" :title="coverageError" type="warning" :closable="false" />
    <p v-if="coverageResult">{{ percent(coverageResult.coverage) }} · {{ coverageResult.available }} / {{ coverageResult.expected }} 根 · 缺失 {{ coverageResult.missing }} 根</p>
  </el-dialog>
</template>
