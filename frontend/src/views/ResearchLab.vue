<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { dateTime, numeric, percent } from '../format'
import ForwardCurve from '../components/ForwardCurve.vue'
import BacktestCandles from '../components/BacktestCandles.vue'

const props = defineProps<{ utc: boolean }>()
type Matched = { variant: string; horizon_s: number; treated: number; pairs: number; blocks: number; mean_difference: number | null; ci95: [number,number] | null }
type Run = { id: string; mode: string; status: string; start_ms: number; end_ms: number; cursor_ms: number | null; error: string | null;
  follow_end_ms?: number; preflight?: { issues?: Gap[]; recorded_gaps?: Gap[] };
  stats: { covered_ticks?: number; asset_ticks?: number; confirmed?: number; rejected?: number; ablation?: { status: string; comparisons: Matched[];
    absolute?: {variant:string;horizon_s:number;samples:number;blocks:number;ci95:[number,number]|null}[];
    bins?: {variant:string;horizon_s:number;feature:string;bucket:string;samples:number;mean_log:number}[]; bin_edges?: Record<string,number[]> } }; source_hash?: string }
type Gap = { asset:string; stream:string; from_ms:number; to_ms:number; reason:string }
type Signal = { id: string; instrument_id: string; symbol: string; variant: string; direction: string; state: string; detected_ms: number; episode_id: string; reasons: string[] }
type Quality = { collector: { heartbeat: string; state: { connected: boolean; subscriptions: number; reconnects: number; late: number; duplicates: number;
    latency_ms: Record<string, number>; out_of_order?: number; unconfirmed_batch_rows?: number; buffer_high_water?: number;
    stage_latency_ms?: Record<string,number>; error?: string } } | null;
  stability?: {samples:number;connected_samples:number}; compute?: {state:Record<string,number>};
  streams: { instrument_id: string; symbol: string; event_type: string; samples: number; first: string; last: string }[] }
type Aggregate = { variant: string; horizon_s: number; attempts: number; complete: number; missing: number; censored: number;
  executable: number; episodes: number; pending: number; depth_failures: number; mean_log: number | null; median_log: number | null; net_return: number | null;
  matched_gross_return: number | null; matched_net_return: number | null; fees_usdc: number | null; spread_usdc: number | null; slippage_usdc: number | null }
type Labels = { horizon_s: number; status: string; detail: { return_log?: number; mfe?: number; mae?: number; coverage?: number; execution?: { status: string; net_return?: number } } }[]
type Detail = { detail: { reference_mid: number; features: Record<string, { value: unknown; coverage: number; quality_flags: string[] }> }; labels: Labels; transitions: { state: string; reasons: string[] }[] }
const defaults = ref('{}')
const parameters = ref('')
const mode = ref<'REPLAY' | 'LIVE'>('REPLAY')
const duration = ref(3600)
const range = ref<[Date, Date]>()
const assets = ref<string[]>([])
const quality = ref<Quality>()
const runs = ref<Run[]>([])
const activeId = ref('')
const current = computed(() => runs.value.find(run => run.id === activeId.value))
const events = ref<Signal[]>([])
const aggregates = ref<Aggregate[]>([])
const metric = ref<'prediction' | 'gross' | 'net'>('prediction')
const diagnostic = computed(()=>Boolean(current.value && ['FAILED','INSUFFICIENT_DATA','STOPPED'].includes(current.value.status)))
const issueRows = computed(()=>[...(current.value?.preflight?.issues ?? []), ...(current.value?.preflight?.recorded_gaps ?? [])])
function metricValue(row: Aggregate) {
  return metric.value === 'gross' ? row.matched_gross_return : metric.value === 'net' ? row.matched_net_return : row.mean_log
}
const visibleSeries = computed(()=>aggregates.value.some(row=>metricValue(row) != null))
const overlap = computed(()=>{
  const c = aggregates.value.filter(row=>row.variant==='C' && metricValue(row)!=null)
  const d = aggregates.value.filter(row=>row.variant==='D' && metricValue(row)!=null)
  return c.length>0 && c.length===d.length && c.every((row,i)=>row.horizon_s===d[i].horizon_s && metricValue(row)===metricValue(d[i]))
})
const offset = ref(0)
const pageSize = 25
const stateFilter = ref('')
const selected = ref<Signal>()
const detail = ref<Detail>()
const raw = ref<{ rows: { event_type: string; available_ts: string; quality: string[]; payload: unknown }[]; truncated: boolean }>()
const visible = ref(false)
const error = ref('')
const busy = ref(false)
const panelStart = computed(() => selected.value ? new Date(Math.floor(selected.value.detected_ms / 60000) * 60000 - 300000).toISOString() : '')
const panelEnd = computed(() => selected.value ? new Date(Math.floor(selected.value.detected_ms / 60000) * 60000 + 360000).toISOString() : '')
const marketOptions = computed(() => [...new Map(quality.value?.streams.map(stream => [stream.instrument_id, stream])).values()])
let polling: ReturnType<typeof setInterval> | undefined
let alive = true
let detailVersion = 0
let queryVersion = 0
let ws: WebSocket | undefined

const title: Record<string,string> = { QUEUED:'排队',RUNNING:'运行中', COMPLETE:'完成', INSUFFICIENT_DATA:'数据不足', STOPPED:'停止', FAILED:'失败',
  CONFIRMED:'确认',REJECTED:'拒绝',PENDING:'待成熟',MISSING:'缺失',CENSORED:'观察期外' }
const groups: Record<string,string> = { A:'A 动量', B:'B + 成交失衡', C:'C + 相对成交量', D:'D + 持续性' }
function time(ms?: number) { return ms == null ? 'N/A' : dateTime(new Date(ms).toISOString(),props.utc) }
function message(cause: unknown) { error.value = (cause as Error).message }
async function refresh() {
  const [feed, listed] = await Promise.all([api<Quality>('/research/quality'),api<Run[]>('/research/runs')])
  if (!alive) return
  quality.value = feed
  runs.value = listed
  if (activeId.value) await loadEvents()
}
async function loadEvents() {
  if (!activeId.value) return
  const version=++queryVersion
  const query=new URLSearchParams({run_id:activeId.value,limit:String(pageSize),offset:String(offset.value)})
  const base='?run_id='+encodeURIComponent(activeId.value)
  if (stateFilter.value) query.set('state',stateFilter.value)
  const [rows,stats]=await Promise.all([api<Signal[]>('/research/events?'+query),api<Aggregate[]>('/research/forward-returns'+base)])
  if (alive && version===queryVersion) { events.value=rows; aggregates.value=stats }
}
function latestRange() {
  let follow = 1800
  try {
    const configured = (JSON.parse(parameters.value).horizons_s ?? []) as unknown[]
    const valid = configured.filter((h): h is number => typeof h === 'number' && Number.isInteger(h) && h>=1 && h<=3600)
    if (valid.length) follow = Math.max(...valid)
  } catch {
    // Invalid draft values are reported on submission.
  }
  const end=Math.floor(Date.now()/1000)*1000-(follow+2)*1000
  range.value=[new Date(end-120000),new Date(end)]
}
async function start() {
  error.value=''
  busy.value=true
  try {
    const value=JSON.parse(parameters.value)
    const config=await api<{hash:string}>('/research/configs','POST',value)
    const draft: Record<string,unknown>={mode:mode.value,config_hash:config.hash,asset_ids:assets.value,duration_s:duration.value}
    if (mode.value==='REPLAY') {
      if (!range.value?.[0] || !range.value[1]) throw Error('请选择历史回放时间')
      draft.start_ms=Math.floor(range.value[0].getTime()/1000)*1000
      draft.end_ms=Math.floor(range.value[1].getTime()/1000)*1000
    }
    const run=await api<Run>(mode.value==='LIVE'?'/research/runs':'/research/experiments','POST',draft)
    activeId.value=run.id
    await refresh()
  } catch (cause) { message(cause) }
  finally { busy.value=false }
}
async function stop() {
  try { await api('/research/runs/'+activeId.value+'/stop','POST'); await refresh() }
  catch(cause) { message(cause) }
}
async function openEvent(signal: Signal) {
  const version=++detailVersion
  selected.value=signal; detail.value=undefined; raw.value=undefined; visible.value=true
  try {
    const [event,replay]=await Promise.all([api<Detail>('/research/events/'+signal.id),
      api<NonNullable<typeof raw.value>>('/research/events/'+signal.id+'/replay')])
    if (version===detailVersion) { detail.value=event; raw.value=replay }
  } catch(cause) { message(cause) }
}
watch(activeId,()=>{offset.value=0;events.value=[];aggregates.value=[];void loadEvents().catch(message)})
watch(stateFilter,()=>{offset.value=0})
watch([offset,stateFilter],()=>void loadEvents().catch(message))
onMounted(async()=>{
  try {
    const detectors=await api<{defaults: object}[]>('/research/detectors')
    defaults.value=parameters.value=JSON.stringify(detectors[0]?.defaults??{},null,2)
    await refresh()
    const initial=marketOptions.value.find(asset=>asset.symbol==='BTC')
    if(initial) assets.value=[initial.instrument_id]
  } catch(cause) {message(cause)}
  if (!alive) return
  polling=setInterval(()=>void refresh().catch(message),10000)
  const protocol=location.protocol==='https:'?'wss:':'ws:'
  ws=new WebSocket(protocol+'//'+location.host+'/api/v1/research/ws')
  ws.onmessage=()=>void refresh().catch(message)
})
onUnmounted(()=>{alive=false;clearInterval(polling);ws?.close()})
</script>

<template>
  <el-alert v-if="error" :title="error" type="error" show-icon @close="error=''" />
  <section class="panel">
    <h2>采集质量</h2>
    <p>RESEARCH MODE · 主网公开行情</p>
    <p v-if="quality?.collector">
      {{ quality.collector.state.connected ? '已连接' : '采集中断' }} · {{ dateTime(quality.collector.heartbeat,utc) }}
      · {{ quality.collector.state.subscriptions ?? 0 }} 条订阅 · {{ quality.collector.state.reconnects }} 次重连
      · P95 {{ numeric(quality.collector.state.latency_ms?.p95,2) }} ms
      · 迟到 {{ numeric(quality.collector.state.late) }} · 重复 {{ numeric(quality.collector.state.duplicates) }}
      · 乱序 {{numeric(quality.collector.state.out_of_order)}} · 未确认批次行 {{numeric(quality.collector.state.unconfirmed_batch_rows)}}
      · 队列峰值 {{numeric(quality.collector.state.buffer_high_water)}}
    </p>
    <p v-if="quality?.collector?.state.stage_latency_ms">
      最近批次排队峰值 / 写入 / 提交确认 / 发布
      {{ ['queue_max','write','commit_ack','publication'].map(k=>numeric(quality?.collector?.state.stage_latency_ms?.[k],2)).join(' / ') }} ms
    </p>
    <p v-if="quality?.compute">最近实时批次特征 / 检测 / 调度滞后
      {{ ['feature_ms','detector_ms','schedule_lag_ms'].map(k=>numeric(quality?.compute?.state[k],2)).join(' / ') }} ms
    </p>
    <p v-if="quality?.stability">24 小时观察窗：{{ quality.stability.samples }} / 1440 个分钟样本，{{ quality.stability.connected_samples }} 个样本已连接</p>
    <el-alert v-if="quality?.collector?.state.error" :title="quality.collector.state.error" type="warning" :closable="false" />
    <el-table :data="quality?.streams ?? []" empty-text="最近一小时暂无研究采集记录" max-height="300">
      <el-table-column prop="symbol" label="交易对" /><el-table-column prop="event_type" label="行情流" />
      <el-table-column prop="samples" label="近 1h 样本" />
      <el-table-column label="首条"><template #default="{ row }">{{ dateTime(row.first,utc) }}</template></el-table-column>
      <el-table-column label="末条"><template #default="{ row }">{{ dateTime(row.last,utc) }}</template></el-table-column>
    </el-table>
  </section>
  <section class="panel">
    <h2>S01 订单流持续性</h2>
    <el-form label-position="top">
      <div class="form-grid">
        <el-form-item label="模式"><el-select v-model="mode"><el-option value="REPLAY" label="历史回放与 A–D 消融" /><el-option value="LIVE" label="实时研究" /></el-select></el-form-item>
        <el-form-item label="研究资产"><el-select v-model="assets" multiple filterable><el-option v-for="item in marketOptions" :key="item.instrument_id" :value="item.instrument_id" :label="item.symbol" /></el-select></el-form-item>
        <el-form-item v-if="mode==='LIVE'" label="检测时长（秒）"><el-input-number v-model="duration" :min="1" :max="14400" /></el-form-item>
        <el-form-item v-else label="历史区间（本地输入）">
          <el-date-picker v-model="range" type="datetimerange" start-placeholder="开始" end-placeholder="结束" />
          <el-button link @click="latestRange">选择已留出标签时间的最近两分钟</el-button>
        </el-form-item>
      </div>
      <el-form-item label="版本化参数（JSON）"><el-input v-model="parameters" type="textarea" :rows="8" spellcheck="false" /></el-form-item>
      <div class="toolbar"><el-button type="primary" :disabled="!assets.length" :loading="busy" @click="start">启动研究</el-button><el-button @click="parameters=defaults">恢复默认</el-button></div>
    </el-form>
  </section>
  <section class="panel">
    <h2>研究任务</h2>
    <div class="toolbar">
      <el-select v-model="activeId" placeholder="选择研究任务" style="width:min(500px,100%)" aria-label="研究任务">
        <el-option v-for="run in runs" :key="run.id" :value="run.id" :label="run.mode+' · '+time(run.start_ms)+' · '+(title[run.status]??run.status)" />
      </el-select>
      <el-button v-if="current && ['QUEUED','RUNNING'].includes(current.status)" @click="stop">停止任务</el-button>
    </div>
    <template v-if="current">
      <p>{{ title[current.status] }} · {{ time(current.start_ms) }} 至 {{ time(current.end_ms) }} · 游标 {{ time(current.cursor_ms ?? undefined) }}</p>
      <p v-if="current.follow_end_ms">计划标签观察至 {{time(current.follow_end_ms)}}</p>
      <p>有效资产秒 {{ numeric(current.stats.covered_ticks ?? 0) }} / {{ numeric(current.stats.asset_ticks ?? 0) }} · 确认 {{ numeric(current.stats.confirmed ?? 0) }} · 拒绝 {{ numeric(current.stats.rejected ?? 0) }}</p>
      <el-alert v-if="current.error" :title="current.error==='Frozen input does not cover one or more label targets.'?'已冻结的数据未覆盖一个或多个标签目标时刻':current.error==='One or more assets did not meet the required warmup, quote or baseline coverage.'?'预热、报价或历史基准覆盖不足':current.error" type="warning" :closable="false" />
      <el-alert v-if="diagnostic" title="任务未完整完成，事件仅供诊断，收益聚合不参与比较" type="warning" :closable="false" />
      <el-collapse v-if="issueRows.length">
        <el-collapse-item :title="'预检与已记录数据缺口 · '+issueRows.length">
          <el-table :data="issueRows">
            <el-table-column prop="asset" label="资产" /><el-table-column prop="stream" label="行情" />
            <el-table-column label="区间"><template #default="{row}">{{time(row.from_ms)}} 至 {{time(row.to_ms)}}</template></el-table-column>
            <el-table-column prop="reason" label="原因" />
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <h2>A–D 消融与未来收益</h2>
      <el-radio-group v-if="aggregates.length" v-model="metric">
        <el-radio-button value="prediction">方向预测</el-radio-button><el-radio-button value="gross">同样本毛收益</el-radio-button><el-radio-button value="net">同样本净收益</el-radio-button>
      </el-radio-group>
      <p v-if="overlap">C 与 D 曲线完全重叠。点击图例可切换显示。</p>
      <ForwardCurve v-if="visibleSeries" :rows="aggregates" :metric="metric" />
      <el-table :data="aggregates" empty-text="暂无确认事件的标签">
        <el-table-column label="实验组"><template #default="{row}">{{groups[row.variant]}}</template></el-table-column>
        <el-table-column prop="horizon_s" label="周期（秒）" /><el-table-column prop="attempts" label="尝试" />
        <el-table-column prop="episodes" label="事件簇" /><el-table-column prop="complete" label="有效" />
        <el-table-column prop="missing" label="缺失" /><el-table-column prop="censored" label="期外" />
        <el-table-column prop="pending" label="待完成" /><el-table-column prop="executable" label="BBO 可执行" />
        <el-table-column prop="depth_failures" label="深度不足" />
        <el-table-column label="平均对数收益"><template #default="{row}">{{percent(row.mean_log)}}</template></el-table-column>
        <el-table-column label="BBO 净收益估计"><template #default="{row}">{{percent(row.net_return)}}</template></el-table-column>
        <el-table-column label="同样本毛收益"><template #default="{row}">{{percent(row.matched_gross_return ?? null)}}</template></el-table-column>
        <el-table-column label="手续费 / 价差 / 滑点 (USDC)"><template #default="{row}">{{[row.fees_usdc,row.spread_usdc,row.slippage_usdc].map(x=>x==null?'N/A':numeric(Number(x),4)).join(' / ')}}</template></el-table-column>
      </el-table>
      <el-collapse v-if="current.stats.ablation">
        <el-collapse-item v-if="current.stats.ablation.absolute?.length" title="各组绝对收益 95% 区间">
          <el-table :data="current.stats.ablation.absolute">
            <el-table-column prop="variant" label="实验组" /><el-table-column prop="horizon_s" label="周期（秒）" />
            <el-table-column prop="samples" label="样本" /><el-table-column prop="blocks" label="时间块" />
            <el-table-column label="95% 区间"><template #default="{row}">{{row.ci95 ? percent(row.ci95[0])+' 至 '+percent(row.ci95[1]) : '统计样本不足'}}</template></el-table-column>
          </el-table>
        </el-collapse-item>
        <el-collapse-item v-if="current.stats.ablation.bins?.length" title="条件分箱">
          <p>分箱编号从 0 开始。边界 {{current.stats.ablation.bin_edges}}</p>
          <el-table :data="current.stats.ablation.bins" max-height="400">
            <el-table-column prop="variant" label="实验组" /><el-table-column prop="horizon_s" label="周期（秒）" />
            <el-table-column prop="feature" label="特征" /><el-table-column prop="bucket" label="分箱" />
            <el-table-column prop="samples" label="样本" />
            <el-table-column label="平均对数收益"><template #default="{row}">{{percent(row.mean_log)}}</template></el-table-column>
          </el-table>
        </el-collapse-item>
        <el-collapse-item title="匹配对照与 95% 区间">
          <p class="muted">与 A 组按同资产、方向、时间块、波动和价差分箱匹配。区间按时间块重采样，数据不足显示 N/A。</p>
          <el-table :data="current.stats.ablation.comparisons" empty-text="该任务暂无可报告的匹配">
            <el-table-column prop="variant" label="实验组" /><el-table-column prop="horizon_s" label="周期（秒）" />
            <el-table-column prop="treated" label="有效实验样本" /><el-table-column prop="pairs" label="配对数" />
            <el-table-column prop="blocks" label="时间块" />
            <el-table-column label="平均收益差"><template #default="{row}">{{percent(row.mean_difference)}}</template></el-table-column>
            <el-table-column label="95% 区间"><template #default="{row}">{{row.ci95 ? percent(row.ci95[0])+' 至 '+percent(row.ci95[1]) : 'N/A'}}</template></el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <h2>事件</h2>
      <el-select v-model="stateFilter" aria-label="事件状态" style="width:130px"><el-option value="" label="全部" /><el-option value="CONFIRMED" label="确认" /><el-option value="REJECTED" label="拒绝" /></el-select>
      <el-table :data="events" empty-text="暂无事件">
        <el-table-column label="可用时间"><template #default="{row}">{{time(row.detected_ms)}}</template></el-table-column>
        <el-table-column prop="symbol" label="交易对" /><el-table-column prop="variant" label="实验组" /><el-table-column prop="direction" label="方向" />
        <el-table-column label="状态"><template #default="{row}">{{title[row.state]}}</template></el-table-column>
        <el-table-column label="查看"><template #default="{row}"><el-button link type="primary" @click="openEvent(row)">详情</el-button></template></el-table-column>
      </el-table>
      <div class="toolbar"><el-button :disabled="!offset" @click="offset-=pageSize">上一页</el-button><el-button :disabled="events.length<pageSize" @click="offset+=pageSize">下一页</el-button></div>
    </template>
  </section>
  <el-dialog v-model="visible" :title="selected?.symbol+' · '+selected?.direction" width="min(980px,95vw)" destroy-on-close>
    <p v-if="selected">{{time(selected.detected_ms)}} · {{title[selected.state]}} · {{selected.reasons.join(' / ')}}</p>
    <template v-if="detail && selected">
      <p>参考中间价 {{ numeric(detail.detail.reference_mid) }} · TFI 15s {{numeric(detail.detail.features.tfi_15?.value as number | null, 4)}} · RVOL 30s {{numeric(detail.detail.features.rvol_30?.value as number | null, 4)}}</p>
      <el-table :data="detail.labels" empty-text="此事件暂无标签">
        <el-table-column prop="horizon_s" label="秒" /><el-table-column label="状态"><template #default="{row}">{{title[row.status]}}</template></el-table-column>
        <el-table-column label="方向收益"><template #default="{row}">{{percent(row.detail.return_log??null)}}</template></el-table-column>
        <el-table-column label="MFE"><template #default="{row}">{{percent(row.detail.mfe??null)}}</template></el-table-column>
        <el-table-column label="MAE"><template #default="{row}">{{percent(row.detail.mae??null)}}</template></el-table-column>
        <el-table-column label="覆盖"><template #default="{row}">{{percent(row.detail.coverage??null)}}</template></el-table-column>
        <el-table-column label="BBO 执行估计"><template #default="{row}">{{row.detail.execution?.status??'N/A'}} · {{percent(row.detail.execution?.net_return??null)}}</template></el-table-column>
      </el-table>
      <BacktestCandles :key="selected.id" :markets="[selected]" interval="1m" :start="panelStart" :end="panelEnd" :utc="utc" title="事件附近 1m K 线" :marker-ms="selected.detected_ms" />
      <el-collapse v-if="raw">
        <el-collapse-item :title="'原始可用时间行情 · '+raw.rows.length+' 条'+(raw.truncated?'（已截断）':'')">
          <pre style="max-height:280px;overflow:auto">{{JSON.stringify(raw.rows,null,2)}}</pre>
        </el-collapse-item>
      </el-collapse>
    </template>
  </el-dialog>
</template>

