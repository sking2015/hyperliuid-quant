<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import { CandlestickSeries, createChart, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { dateTime, kinds, numeric, percent } from '../format'
import type { Bar, Kind, Market, Pool } from '../types/models'

const props = defineProps<{ utc: boolean }>()
const search = ref('')
const kind = ref<Kind | ''>(''); const page = ref(1); const total = ref(0)
const items = ref<Market[]>([]); const pools = ref<Pool[]>([]); const pool = ref('')
const error = ref(''); const loading = ref(false)
const sort = ref('volume_24h'); const direction = ref('desc')
const selected = ref<Market>(); const interval = ref('1m'); const visible = ref(false)
const chartTarget = ref<HTMLElement>(); const chartError = ref('')
const coverage = ref<{ gaps: unknown[] }>()
const lastBar = ref<Bar>()
let chart: IChartApi | undefined
let timer: ReturnType<typeof setInterval>
let queryVersion = 0; let chartVersion = 0

async function load(background = false) {
  const revision = ++queryVersion
  if (!background) loading.value = true
  const params = new URLSearchParams({ q: search.value, offset: String((page.value - 1) * 25),
    limit: '25', sort: sort.value, direction: direction.value })
  if (kind.value) params.set('market_type', kind.value)
  try {
    const data = await api<{ items: Market[]; total: number }>('/markets?' + params)
    if (revision !== queryVersion) return
    items.value = data.items
    total.value = data.total
    error.value = ''
  } catch (cause) {
    if (revision === queryVersion) error.value = (cause as Error).message
  } finally {
    if (revision === queryVersion) loading.value = false
  }
}
function changeSort(change: { prop: string; order: string | null }) {
  sort.value = change.order ? change.prop : 'volume_24h'
  direction.value = change.order === 'ascending' ? 'asc' : 'desc'
  if (page.value !== 1) page.value = 1
  else void load()
}
async function changeMember(market: Market, add: boolean) {
  try {
    await api('/watchlists/' + pool.value + '/members/' + encodeURIComponent(market.instrument_id), add ? 'POST' : 'DELETE')
    ElMessage.success(add ? '已手动加入观察池' : '已移除手动成员来源')
  } catch (cause) { ElMessage.error((cause as Error).message) }
}
async function draw() {
  if (!selected.value || !visible.value) return
  const revision = ++chartVersion
  const path = '/markets/' + encodeURIComponent(selected.value.instrument_id)
  chartError.value = ''
  lastBar.value = undefined
  coverage.value = undefined
  chart?.remove()
  chart = undefined
  try {
    const [bars, result] = await Promise.all([api<Bar[]>(path + '/candles?interval=' + interval.value),
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
    chart.addSeries(CandlestickSeries, { upColor: up, downColor: down,
      borderVisible: false, wickUpColor: up, wickDownColor: down }).setData(bars.map(b => ({
      time: Math.floor(Date.parse(b.open_ts) / 1000) as UTCTimestamp,
      open: Number(b.o), high: Number(b.h), low: Number(b.l), close: Number(b.c),
    })))
    chart.timeScale().fitContent()
  } catch (cause) {
    if (revision === chartVersion) chartError.value = (cause as Error).message
  }
}
function show(row: Market) { selected.value = row; visible.value = true }
function closeChart() { chartVersion++; chart?.remove(); chart = undefined }
watch([search, kind], () => { if (page.value !== 1) page.value = 1; else void load() })
watch(page, () => void load())
watch([interval, () => props.utc], () => void draw())
onMounted(async () => {
  void load()
  timer = setInterval(() => void load(true), 15_000)
  try { pools.value = await api<Pool[]>('/watchlists') } catch (cause) { ElMessage.error((cause as Error).message) }
})
onUnmounted(() => { clearInterval(timer); closeChart() })
</script>

<template>
  <section class="panel">
    <div class="toolbar">
      <el-input v-model="search" clearable placeholder="搜索交易对或资产代码" aria-label="搜索交易对" style="width:230px" />
      <el-select v-model="kind" aria-label="市场分类" placeholder="全部市场" style="width:145px">
        <el-option label="全部市场" value="" /><el-option v-for="(label, value) in kinds" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select v-model="pool" clearable placeholder="选择操作目标池" aria-label="目标观察池" style="width:180px">
        <el-option v-for="item in pools" :key="item.id" :label="item.name" :value="item.id" />
      </el-select>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="instrument_id" empty-text="暂无市场，请检查采集状态"
      :default-sort="{ prop: 'volume_24h', order: 'descending' }" @sort-change="changeSort">
      <el-table-column prop="symbol" label="交易对" sortable="custom" min-width="190">
        <template #default="{ row }"><el-button link type="primary" @click="show(row)">{{ row.symbol }}</el-button>
          <div class="muted">{{ kinds[(row as Market).market_type] }} · {{ row.status === 'DELISTED' ? '已下线' : row.paper_supported ? '可用于后续模拟' : '仅观察' }}</div></template>
      </el-table-column>
      <el-table-column prop="mark" label="标记价（计价币）" sortable="custom" min-width="140"><template #default="{ row }">{{ numeric(row.mark) }} {{ row.mark == null ? '' : row.quote }}</template></el-table-column>
      <el-table-column prop="volume_24h" label="24h 成交额（计价币）" sortable="custom" min-width="175"><template #default="{ row }">{{ numeric(row.volume_24h, 2) }} {{ row.volume_24h == null ? '' : row.quote }}</template></el-table-column>
      <el-table-column prop="change_24h" label="24h 涨跌幅" sortable="custom" min-width="125"><template #default="{ row }"><span :class="{up: row.change_24h > 0, down: row.change_24h < 0}">{{ percent(row.change_24h) }}</span></template></el-table-column>
      <el-table-column prop="market_cap" label="流通市值（USD）" sortable="custom" min-width="150"><template #default="{ row }">{{ numeric(row.market_cap, 2) }}</template></el-table-column>
      <el-table-column label="数据时间" min-width="185"><template #default="{ row }">{{ dateTime(row.as_of, utc) }}<div v-if="row.stale" class="muted">过期或缺失</div></template></el-table-column>
      <el-table-column v-if="pool" label="观察池" width="120"><template #default="{ row }"><el-button link type="primary" @click="changeMember(row, true)">加入</el-button><el-button link @click="changeMember(row, false)">移除</el-button></template></el-table-column>
    </el-table>
    <div class="paging"><el-pagination v-model:current-page="page" :page-size="25" :total="total" layout="total, prev, pager, next" /></div>
  </section>
  <el-dialog v-model="visible" :title="selected?.symbol" width="min(900px, 94vw)" @opened="draw" @closed="closeChart">
    <div v-if="selected" class="toolbar">
      <el-select v-model="interval" aria-label="K 线周期" style="width:100px"><el-option v-for="i in ['1m','3m','5m','15m','1h','4h','1d']" :key="i" :value="i" /></el-select>
      <span>资金费率 {{ percent(selected.funding) }}</span><span>OI（基础资产）{{ numeric(selected.open_interest) }}</span><span>价差 {{ numeric(selected.spread_bps, 2) }} bps</span>
    </div>
    <el-alert v-if="chartError" :title="chartError" type="info" :closable="false" />
    <div ref="chartTarget" class="chart" v-show="!chartError" />
    <p v-if="lastBar">{{ dateTime(lastBar.open_ts, utc) }} · {{ lastBar.is_closed ? '已闭合' : '未闭合' }} · 量 {{ numeric(lastBar.v) }}（基础资产）</p>
    <p v-if="coverage">近期记录的行情缺口 {{ coverage.gaps.length }} 条（最多显示 100 条）</p>
  </el-dialog>
</template>

