<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, ApiError } from '../api/client'
import StrategyFields from '../components/StrategyFields.vue'
import MarketDialog from '../components/MarketDialog.vue'
import EquityChart from '../components/EquityChart.vue'
import BacktestCandles from '../components/BacktestCandles.vue'
import { dateTime, numeric, percent } from '../format'
import type { Market, MarketIdentity, Pool } from '../types/models'
import { reasons, type AutoBacktest, type BacktestDraft, type Manifest, type Parameters, type Report, type Run, type StrategyDefinition, type Trade } from '../types/backtest'

const props = defineProps<{ utc: boolean; preset?: { id: string; params: Parameters } }>()
const strategies = ref<StrategyDefinition[]>([])
const strategyId = ref('')
const params = ref<Parameters>({})
const definition = computed(() => strategies.value.find(item => item.id === strategyId.value))
watch(definition, item => {
  if (item) params.value = { ...item.defaults, ...(item.id === props.preset?.id ? props.preset.params : {}) }
})
const pools = ref<Pool[]>([])
const mode = ref<'markets' | 'pool'>('markets')
const ids = ref<string[]>([])
const pool = ref('')
const marketOptions = ref<Market[]>([])
const range = ref<[Date, Date]>()
const fee = ref(0.05)
const slippage = ref(0.05)
const strictFunding = ref(false)
const manifest = ref<Manifest>()
const candleBlocked = computed(() => manifest.value?.coverage.some(row => row.missing > 0 || (row.invalid ?? 0) > 0))
const fundingBlocked = computed(() => manifest.value?.require_funding && manifest.value.funding_partial)
function applySuggestedRange() {
  const suggested = manifest.value?.suggested_range
  if (suggested) range.value = [new Date(suggested.start), new Date(suggested.end)]
}
watch([strategyId, params, mode, ids, pool, range, strictFunding], () => { manifest.value = undefined }, { deep: true })
const runs = ref<Run[]>([])
const selected = ref<Run>()
const autoSelection = ref<AutoBacktest>()
const report = ref<Report>()
const instrument = ref<MarketIdentity>()
const trade = ref<Trade>()
const error = ref('')
const busy = ref(false)
let poll: ReturnType<typeof setInterval>
let revision = 0
let searchRevision = 0
let stopping = false

const metrics = [
  ['net_pnl', '净盈亏（USDC）'], ['return_rate', '收益率'], ['win_rate', '胜率'], ['max_drawdown', '最大回撤'],
  ['profit_factor', 'Profit Factor'], ['trade_count', '交易次数'], ['fees', '手续费（USDC）'], ['funding', '资金费率净额（USDC）'],
]
const stateLabels = { QUEUED: '排队中', RUNNING: '运行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' }
function displayMetric(key: string, value: string | number | null) {
  if (value == null) return 'N/A'
  return ['return_rate', 'win_rate', 'max_drawdown'].includes(key) ? numeric(Number(value) * 100, 2) + '%' : numeric(value, 4)
}
async function search(query: string) {
  const current = ++searchRevision
  try {
    const result = await api<{items: Market[]}>('/markets?market_type=PERP&limit=30&q=' + encodeURIComponent(query))
    if (current !== searchRevision) return
    marketOptions.value = [...new Map([...marketOptions.value.filter(m => ids.value.includes(m.instrument_id)),
      ...result.items].map(m => [m.instrument_id, m])).values()]
  } catch (cause) { if (current === searchRevision) error.value = (cause as Error).message }
}
function settings(): Omit<BacktestDraft, "start" | "end"> {
  return { strategy_id: strategyId.value, parameters: params.value,
    instrument_ids: mode.value === 'markets' ? ids.value : [],
    watchlist_id: mode.value === 'pool' ? pool.value || null : null,
    fee_rate: fee.value / 100, slippage: slippage.value / 100, require_funding: strictFunding.value }
}
function draft(): BacktestDraft {
  if (!range.value?.[0] || !range.value[1]) throw Error('请选择起止时间')
  return { ...settings(), start: range.value[0].toISOString(), end: range.value[1].toISOString() }
}
async function list() {
  const response = await api<Run[]>('/backtests')
  if (stopping) return
  runs.value = response
  const fresh = response.find(item => item.id === selected.value?.id)
  if (fresh && (fresh.status !== selected.value?.status || fresh.progress !== selected.value?.progress)) {
    if (fresh.status === 'SUCCEEDED') await open(fresh)
    else selected.value = fresh
  }
}
async function open(run: Run) {
  const version = ++revision
  selected.value = run
  report.value = undefined
  if (run.status === 'SUCCEEDED') {
    try {
      const result = await api<Report>('/backtests/' + run.id + '/report')
      if (version === revision) report.value = result
    } catch (cause) { if (version === revision) error.value = (cause as Error).message }
  }
}
async function action(start: boolean, automatic = false) {
  busy.value = true
  error.value = ''
  manifest.value = undefined
  try {
    if (!start) { manifest.value = await api<Manifest>('/backtests/preflight', 'POST', draft()); return }
    const automaticTask = automatic ? await api<AutoBacktest>('/backtests/auto', 'POST', settings()) : undefined
    if (automaticTask) {
      range.value = [new Date(automaticTask.start), new Date(automaticTask.end)]
      autoSelection.value = automaticTask
    }
    const task = automaticTask ?? await api<{ id: string }>('/backtests', 'POST', draft())
    await open(await api<Run>('/backtests/' + task.id))
    await list()
  } catch (cause) {
    const details = cause instanceof ApiError && cause.status === 422 ? cause.details as Partial<Manifest> | null : null
    if (details?.ready === false && Array.isArray(details.coverage)) manifest.value = details as Manifest
    else error.value = (cause as Error).message
  }
  finally { busy.value = false }
}
async function cancel(run: Run) {
  try { await api('/backtests/' + run.id + '/cancel', 'POST'); await list() }
  catch (cause) { error.value = (cause as Error).message }
}
function symbol(id: string) { return selected.value?.configuration.markets.find(m => m.instrument_id === id)?.symbol ?? id }
function summarizeTrades({ columns, data }: { columns: { property?: string }[]; data: Trade[] }) {
  const total = data.reduce((sum, trade) => sum + Number(trade.net_pnl), 0)
  return columns.map((column, index) => {
    if (index === 0) return '总计'
    return column.property === 'net_pnl' ? `${numeric(total, 4)} USDC` : ''
  })
}
function showTrade(row: Trade) {
  trade.value = row
  instrument.value = { instrument_id: row.instrument_id, symbol: symbol(row.instrument_id) }
}
onMounted(async () => {
  try {
    [strategies.value, pools.value] = await Promise.all([api<StrategyDefinition[]>('/strategies'), api<Pool[]>('/watchlists')])
    strategyId.value = props.preset?.id ?? strategies.value[0]?.id ?? ''
    await Promise.all([search(''), list()])
  } catch (cause) { error.value = (cause as Error).message }
  if (!stopping) poll = setInterval(() => void list().catch(cause => { error.value = (cause as Error).message }), 2000)
})
onUnmounted(() => { stopping = true; clearInterval(poll) })
</script>

<template>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="panel">
    <h2>新建回测</h2>
    <el-form label-position="top">
      <div class="form-grid">
        <el-form-item label="策略"><el-select v-model="strategyId"><el-option v-for="item in strategies" :key="item.id" :value="item.id" :label="item.name" /></el-select></el-form-item>
        <el-form-item label="资产来源"><el-radio-group v-model="mode"><el-radio-button value="markets">交易对</el-radio-button><el-radio-button value="pool">观察池</el-radio-button></el-radio-group></el-form-item>
        <el-form-item v-if="mode === 'markets'" label="原生 USDC 永续">
          <el-select v-model="ids" multiple filterable remote :remote-method="search" :multiple-limit="20">
            <el-option v-for="item in marketOptions" :key="item.instrument_id" :value="item.instrument_id" :label="item.symbol" />
          </el-select>
        </el-form-item>
        <el-form-item v-else label="固定观察池快照">
          <el-select v-model="pool"><el-option v-for="item in pools" :key="item.id" :value="item.id" :label="item.name" /></el-select>
        </el-form-item>
      </div>
      <el-form-item label="历史范围（本地输入，提交时转 UTC，起止须和周期对齐）">
        <el-date-picker v-model="range" type="datetimerange" start-placeholder="开始" end-placeholder="结束" />
      </el-form-item>
      <StrategyFields v-if="definition" :definition="definition" v-model="params" />
      <div class="form-grid">
        <el-form-item label="单边手续费（%）"><el-input-number v-model="fee" :min="0" :max="5" :step="0.01" /></el-form-item>
        <el-form-item label="单边滑点（%）"><el-input-number v-model="slippage" :min="0" :max="5" :step="0.01" /></el-form-item>
        <el-form-item label="资金费率"><el-checkbox v-model="strictFunding">要求覆盖全部小时</el-checkbox></el-form-item>
      </div>
      <div class="toolbar">
        <el-button :disabled="busy || !definition" @click="action(false)">检查数据覆盖</el-button>
        <el-button :disabled="busy || !definition" @click="action(true)">发起回测</el-button>
        <el-button type="primary" :disabled="!definition || !(mode === 'markets' ? ids.length : pool)"
          :loading="busy" @click="action(true, true)">选择可用时段并回测</el-button>
      </div>
    </el-form>
    <section v-if="autoSelection" aria-label="自动选择的回测时段">
      <h3>最近一次自动选择</h3>
      <p>{{ dateTime(autoSelection.start, utc) }} 至 {{ dateTime(autoSelection.end, utc) }}
        （{{ utc ? 'UTC' : '本地时间' }}，终点不计入） · {{ autoSelection.interval }}</p>
      <p v-for="asset in autoSelection.coverage" :key="asset.instrument_id">
        {{ asset.symbol }} · <strong>{{ numeric(asset.bars) }} 根闭合 K 线</strong>
        <span v-if="asset.warmup_bars" class="muted"> · 另有 {{ numeric(asset.warmup_bars) }} 根预热 K 线</span>
      </p>
    </section>
    <div v-if="manifest">
      <el-alert v-if="!manifest.ready"
        :title="candleBlocked ? `所选时段缺少完整的 ${manifest.interval ?? params.timeframe} 闭合 K 线` : '严格资金费率检查未通过'"
        type="warning" :closable="false" />
      <p v-if="candleBlocked">优先调整「历史范围」到连续覆盖的时段。也可换一个已覆盖的「K 线周期」，或移除数据不足的交易对后重新预检。</p>
      <p v-if="manifest.warmup_bars && manifest.coverage.some(row => (row.missing_warmup ?? 0) > 0)">
        EMA 需要在回测开始前为每个资产预留 {{ manifest.warmup_bars }} 根闭合 K 线。可以推迟开始时间、调小「慢 EMA」（仍须大于快 EMA），或补齐更早的数据。
      </p>
      <p v-if="fundingBlocked">若接受仅按已记录的结算时段计费，可取消「要求覆盖全部小时」。否则请缩短时段或补齐资金费率后再检查。</p>
      <p v-else-if="manifest.funding_partial" class="muted">历史资金费率有缺口。当前未要求完整覆盖，回测将只计入可用记录。</p>
      <p v-if="manifest.ready">预检通过 · 每个资产预热 {{ manifest.warmup_bars }} 根</p>
      <div v-if="manifest.suggested_range && candleBlocked" class="toolbar">
        <span>各资产共同的连续 K 线时段（已预留预热，本地时间）{{ dateTime(manifest.suggested_range.start) }} 至 {{ dateTime(manifest.suggested_range.end) }}，终点不计入</span>
        <el-button @click="applySuggestedRange">填入建议时段</el-button>
      </div>
      <p v-else-if="candleBlocked">近期本地数据中未找到能满足预热并覆盖所有资产的共同连续时段。可减少资产、缩短预热或导入缺失 K 线。</p>
      <el-table :data="manifest.coverage">
        <el-table-column label="交易对" min-width="104"><template #default="{ row }">{{ row.symbol ?? marketOptions.find(item => item.instrument_id === row.instrument_id)?.symbol ?? row.instrument_id }}</template></el-table-column>
        <el-table-column label="闭合 K 线（含预热）" min-width="165"><template #default="{ row }">{{ numeric(row.available) }} / {{ numeric(row.expected) }}</template></el-table-column>
        <el-table-column prop="missing_requested" label="回测段缺失" min-width="112" />
        <el-table-column prop="missing_warmup" label="预热缺失" min-width="100" />
        <el-table-column prop="missing_funding_hours" label="缺失结算小时" min-width="115" />
        <el-table-column v-if="manifest.coverage.some(row => row.invalid)" prop="invalid" label="无效 K 线" min-width="100" />
        <el-table-column v-if="!manifest.ready" label="首根缺失 K 线（本地）" min-width="175"><template #default="{ row }">{{ dateTime(row.first_missing) }}</template></el-table-column>
        <el-table-column v-if="!manifest.ready" label="近期本地数据边界（本地）" min-width="215">
          <template #default="{ row }">{{ row.local_start ? dateTime(row.local_start) + ' 至 ' + dateTime(row.local_end) : '暂无该周期闭合 K 线' }}</template>
        </el-table-column>
      </el-table>
    </div>
  </section>
  <section class="panel">
    <h2>回测记录</h2>
    <el-table :data="runs" empty-text="暂无回测">
      <el-table-column label="提交时间" min-width="150"><template #default="{ row }">{{ dateTime(row.created_at, utc) }}</template></el-table-column>
      <el-table-column prop="configuration.strategy_id" label="策略" min-width="125" />
      <el-table-column label="状态及进度" min-width="190"><template #default="{ row }">{{ stateLabels[(row as Run).status] }} <el-progress :percentage="row.progress" /></template></el-table-column>
      <el-table-column label="操作" width="135"><template #default="{ row }">
        <el-button link type="primary" @click="open(row)">查看</el-button>
        <el-button v-if="['QUEUED','RUNNING'].includes(row.status)" link @click="cancel(row)">取消</el-button>
      </template></el-table-column>
    </el-table>
  </section>
  <section v-if="selected" class="panel">
    <h2>回测结果</h2>
    <p>{{ stateLabels[selected.status] }} · 策略 v{{ selected.configuration.strategy_version }} · {{ selected.progress }}%</p>
    <el-alert v-if="selected.error" :title="selected.error" type="error" :closable="false" />
    <template v-if="report">
      <el-alert v-if="report.manifest.funding_partial" title="部分历史资金费率缺失，结果只计入可用结算记录。" type="warning" :closable="false" />
      <dl class="metrics"><div v-for="[key, label] in metrics" :key="key"><dt>{{ label }}</dt><dd>{{ displayMetric(key, report.metrics[key]) }}</dd></div></dl>
      <h2>权益曲线</h2><EquityChart :points="report.equity_curve" :utc="utc" />
      <BacktestCandles :key="selected.id" :markets="selected.configuration.markets"
        :interval="String(selected.configuration.parameters.timeframe)"
        :start="selected.configuration.start" :end="selected.configuration.end" :utc="utc" />
      <p class="muted">L1 下一根开盘 · 同根冲突止损优先（{{ report.metrics.intrabar_conflicts }} 次）· 期末持仓按最后收盘估值</p>
      <p v-if="selected.configuration.parameters.exit_mode === 'TRAILING'" class="muted">
        移动止损 · 回撤 {{ percent(Number(selected.configuration.parameters.trailing_stop ?? 0.05)) }} · 新极值从下一根 K 线起生效
      </p>
      <p class="muted">K 线来源 {{ report.manifest.candle_sources.join(' / ') }} · 数据 SHA-256 {{ report.manifest.sha256 }}</p>
      <div class="toolbar"><h2>交易明细</h2><a :href="'/api/v1/backtests/' + selected.id + '/trades.csv'">导出 CSV</a></div>
      <el-table :data="report.trades" empty-text="暂无已平仓交易" show-summary
        :summary-method="summarizeTrades" @row-click="showTrade">
        <el-table-column label="交易对" min-width="100"><template #default="{ row }"><el-button link type="primary" @click.stop="showTrade(row)">{{ symbol(row.instrument_id) }}</el-button></template></el-table-column>
        <el-table-column prop="side" label="方向" width="85" />
        <el-table-column label="开仓时间" min-width="145"><template #default="{ row }">{{ dateTime(row.entry_time, utc) }}</template></el-table-column>
        <el-table-column label="平仓时间" min-width="145"><template #default="{ row }">{{ dateTime(row.exit_time, utc) }}</template></el-table-column>
        <el-table-column label="开仓价" min-width="105"><template #default="{ row }">{{ numeric(row.entry) }}</template></el-table-column>
        <el-table-column label="平仓价" min-width="105"><template #default="{ row }">{{ numeric(row.exit) }}</template></el-table-column>
        <el-table-column label="数量" min-width="95"><template #default="{ row }">{{ numeric(row.quantity) }}</template></el-table-column>
        <el-table-column label="手续费" min-width="95"><template #default="{ row }">{{ numeric(row.fees, 4) }}</template></el-table-column>
        <el-table-column label="资金费率净额" min-width="115"><template #default="{ row }">{{ numeric(row.funding, 5) }}</template></el-table-column>
        <el-table-column prop="net_pnl" label="净盈亏" min-width="130"><template #default="{ row }">{{ numeric(row.net_pnl, 4) }}</template></el-table-column>
        <el-table-column label="原因" min-width="100"><template #default="{ row }">{{ reasons[row.reason] ?? row.reason }}</template></el-table-column>
      </el-table>
      <el-collapse v-if="report.signal_research"><el-collapse-item title="信号研究">
        <el-table :data="report.signal_research">
          <el-table-column prop="minutes" label="观察窗口（分钟）" /><el-table-column prop="samples" label="样本数" />
          <el-table-column label="平均方向收益"><template #default="{ row }">{{ displayMetric('return_rate', row.return_rate) }}</template></el-table-column>
          <el-table-column label="平均 MFE"><template #default="{ row }">{{ displayMetric('return_rate', row.mfe) }}</template></el-table-column>
          <el-table-column label="平均 MAE"><template #default="{ row }">{{ displayMetric('return_rate', row.mae) }}</template></el-table-column>
        </el-table>
      </el-collapse-item></el-collapse>
      <h2>期末未平仓</h2>
      <el-table :data="report.open_positions" empty-text="无未平仓">
        <el-table-column prop="instrument_id" label="资产" /><el-table-column prop="side" label="方向" />
        <el-table-column label="未实现盈亏"><template #default="{ row }">{{ numeric(row.unrealized, 4) }}</template></el-table-column>
      </el-table>
    </template>
  </section>
  <MarketDialog :instrument="instrument" :utc="utc" :trade="trade"
    :trade-interval="String(selected?.configuration.parameters.timeframe ?? '15m')"
    @close="instrument = undefined; trade = undefined" />
</template>

