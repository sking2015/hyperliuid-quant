<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { CandlestickSeries, createChart, createSeriesMarkers, type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import { api } from '../api/client'
import { dateTime, numeric } from '../format'
import { minutes } from '../types/backtest'
import type { Bar, MarketIdentity } from '../types/models'

const props = defineProps<{ markets: MarketIdentity[]; interval: string; start: string; end: string; utc: boolean; title?: string; markerMs?: number }>()
const selected = ref(props.markets[0]?.instrument_id ?? '')
const symbol = computed(() => props.markets.find(market => market.instrument_id === selected.value)?.symbol)
const target = ref<HTMLElement>()
const count = ref(0)
const loading = ref(false)
const error = ref('')
let chart: IChartApi | undefined
let series: ISeriesApi<'Candlestick'> | undefined
let revision = 0

async function load() {
  if (!series || !selected.value) return
  const version = ++revision
  const path = '/markets/' + encodeURIComponent(selected.value) + '/candles?'
  const start = Date.parse(props.start)
  const end = Date.parse(props.end)
  const step = minutes[props.interval] * 60_000
  const chunk = 5000 * step
  loading.value = true
  count.value = 0
  error.value = ''
  series.setData([])
  try {
    if (!Number.isFinite(chunk) || chunk <= 0 || !(start < end))
      throw new Error('回测记录中缺少有效的 K 线周期或时段')
    const bars: Bar[] = []
    for (let left = start; left < end; left += chunk) {
      const query = new URLSearchParams({
        interval: props.interval,
        start: new Date(left).toISOString(),
        end: new Date(Math.min(left + chunk, end)).toISOString(),
        limit: '5000',
      })
      const batch = await api<Bar[]>(path + query)
      if (version !== revision) return
      bars.push(...batch.filter(bar => bar.is_closed))
    }
    series.setData(bars.map(bar => ({
      time: (Date.parse(bar.open_ts) / 1000) as UTCTimestamp,
      open: Number(bar.o), high: Number(bar.h), low: Number(bar.l), close: Number(bar.c),
    })))
    if (props.markerMs != null) {
      const marker = Math.floor(props.markerMs / step) * step
      if (bars.some(bar => Date.parse(bar.open_ts) === marker)) {
        createSeriesMarkers(series, [{time:(marker/1000) as UTCTimestamp,position:'aboveBar',
          shape:'arrowDown',color:'#366480',text:'研究事件'}])
      }
    }
    count.value = bars.length
    chart?.timeScale().fitContent()
  } catch (cause) {
    if (version === revision) error.value = (cause as Error).message
  } finally {
    if (version === revision) loading.value = false
  }
}

function formatChartTime(time: number) {
  return dateTime(new Date(time * 1000).toISOString(), props.utc)
}

onMounted(() => {
  if (!target.value) return
  chart = createChart(target.value, {
    autoSize: true, timeScale: { timeVisible: true },
    localization: { timeFormatter: formatChartTime },
  })
  const style = getComputedStyle(target.value)
  const up = style.getPropertyValue('--up').trim()
  const down = style.getPropertyValue('--down').trim()
  series = chart.addSeries(CandlestickSeries, {
    upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down,
  })
  void load()
})
watch(selected, () => void load())
watch(() => props.utc, () => chart?.applyOptions({ localization: { timeFormatter: formatChartTime } }))
onUnmounted(() => { revision++; chart?.remove(); chart = undefined; series = undefined })
</script>

<template>
  <section  :aria-label="title ?? '回测时段 K 线'">
    <div class="toolbar">
      <h2>{{ title ?? '回测时段 K 线' }}</h2>
      <el-select v-if="markets.length > 1" v-model="selected" aria-label="图表交易对" style="width:160px">
        <el-option v-for="market in markets" :key="market.instrument_id" :value="market.instrument_id" :label="market.symbol" />
      </el-select>
      <span>{{ symbol }} · {{ interval }}</span>
    </div>
    <p class="muted">{{ dateTime(start, utc) }} 至 {{ dateTime(end, utc) }}（{{ utc ? 'UTC' : '本地时间' }}，终点不计入）<span v-if="!loading && count"> · {{ numeric(count) }} 根闭合 K 线</span></p>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <p v-else-if="!loading && !count" class="muted">当前库中暂无该回测时段的闭合 K 线</p>
    <div ref="target" v-loading="loading" class="chart" role="img" :aria-label="symbol + ' ' + interval + ' 回测 K 线图'" />
  </section>
</template>