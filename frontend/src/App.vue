<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api/client'
import { dateTime, kinds, numeric, percent } from './format'
import type { Status } from './types/models'
import Markets from './views/Markets.vue'
import Watchlists from './views/Watchlists.vue'

const Strategies = defineAsyncComponent(() => import('./views/Strategies.vue'))
const Backtests = defineAsyncComponent(() => import('./views/Backtests.vue'))
const page = ref<'markets' | 'watchlists' | 'strategies' | 'backtests' | 'system'>('markets')
const preset = ref<{ id: string; params: Record<string, number | string> }>()

function useStrategy(id: string, params: Record<string, number | string>) {
  preset.value = { id, params }
  page.value = 'backtests'
}
const labels = { markets: '市场', watchlists: '观察池', strategies: '策略', backtests: '回测', system: '系统' }
const utc = ref(true)
const classic = ref(false)
const status = ref<Status>()
const error = ref('')
const projectBytes = computed(() => {
  const state = status.value?.worker?.state
  if (state?.database_bytes == null || state.data_file_bytes == null) return null
  return state.database_bytes + state.data_file_bytes
})

function storageSize(bytes: number | null | undefined) {
  if (bytes == null) return 'N/A'
  if (bytes < 2 ** 20) return numeric(bytes / 2 ** 10, 1) + ' KiB'
  if (bytes < 2 ** 30) return numeric(bytes / 2 ** 20, 1) + ' MiB'
  return numeric(bytes / 2 ** 30, 2) + ' GiB'
}
let socket: WebSocket | undefined
let retry: ReturnType<typeof setTimeout>
let stopped = false

async function sync() {
  try {
    status.value = await api<Status>('/system/status')
    error.value = ''
  } catch (cause) {
    error.value = (cause as Error).message
  }
}
function connect() {
  let seq = 0
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  socket = new WebSocket(protocol + '//' + location.host + '/api/v1/ws/events')
  socket.onopen = sync
  socket.onmessage = ({ data }) => {
    const event = JSON.parse(data)
    if (event.seq !== seq + 1) void sync()
    else if (event.type === 'system_status') {
      status.value = event.payload
      error.value = ''
    }
    seq = event.seq
  }
  socket.onclose = () => {
    if (!stopped) {
      void sync()
      retry = setTimeout(connect, 2500)
    }
  }
}
onMounted(() => { void sync(); connect() })
onUnmounted(() => { stopped = true; clearTimeout(retry); socket?.close() })
</script>

<template>
  <div class="app-shell" :class="{classic}">
    <aside>
      <div class="brand"><span class="brand-icon">HL</span><strong>Quant Lab</strong></div>
      <nav aria-label="主导航">
        <button v-for="(label, key) in labels" :key="key" :class="{active: page === key}" @click="page = key">{{ label }}</button>
      </nav>
      <div class="aside-foot"><span class="paper">PAPER MODE</span><small>Hyperliquid 主网只读行情</small></div>
    </aside>
    <main>
      <header class="top">
        <h1>{{ labels[page] }}</h1>
        <div class="toolbar">
          <el-checkbox v-model="classic">红涨绿跌</el-checkbox>
          <el-switch v-model="utc" active-text="UTC" inactive-text="本地" />
          <span class="connection" :class="{online: status && !status.worker_stale && status.worker?.state.ws_connected}">{{ status && !status.worker_stale && status.worker?.state.ws_connected ? '行情已连接' : '行情未连接' }}</span>
        </div>
      </header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <Markets v-if="page === 'markets'" :utc="utc" />
      <Watchlists v-if="page === 'watchlists'" :utc="utc" />
      <Strategies v-if="page === 'strategies'" @backtest="useStrategy" />
      <Backtests v-if="page === 'backtests'" :utc="utc" :preset="preset" />
      <section v-if="page === 'system'" class="panel">
        <div v-if="status">
          <dl class="metrics">
            <div v-for="item in status.counts" :key="item.market_type"><dt>{{ kinds[item.market_type] }}</dt><dd>{{ numeric(item.count) }}</dd></div>
            <div><dt>观察资产</dt><dd>{{ numeric(status.observed) }}</dd></div>
            <div><dt>首次补齐未完成</dt><dd>{{ numeric(status.coverage.bootstrap_remaining) }}</dd></div>
            <div><dt>首次补齐已到期</dt><dd>{{ numeric(status.coverage.bootstrap_due) }}</dd></div>
            <div><dt>周期续采已到期</dt><dd>{{ numeric(status.coverage.recurring_due) }}</dd></div>
            <div><dt>观察池 5 分钟无闭合 1m K 线</dt><dd>{{ numeric(status.integrity?.stale_observed_1m) }}</dd></div>
            <div><dt>WS 行情延迟 (ms)</dt><dd>{{ numeric(status.worker?.state.ws_latency_ms) }}</dd></div>
            <div><dt>上游 API 错误率（近 5 分钟）</dt><dd>{{ percent(status.worker?.state.upstream_api?.error_rate ?? null) }}</dd></div>
            <div><dt>本地 API 5xx（近 5 分钟）</dt><dd>{{ percent(status.api_metrics?.error_rate ?? null) }}</dd></div>
            <div><dt>行情订阅</dt><dd>{{ numeric(status.worker?.state.subscriptions) }}</dd></div>
            <div><dt>订阅池引用</dt><dd>{{ numeric(status.worker?.state.subscription_references) }}</dd></div>
            <div><dt>重连次数</dt><dd>{{ numeric(status.worker?.state.ws_reconnects) }}</dd></div>
            <div><dt>项目数据总占用</dt><dd>{{ storageSize(projectBytes) }}</dd></div>
            <div><dt>PostgreSQL 数据库占用</dt><dd>{{ storageSize(status.worker?.state.database_bytes) }}</dd></div>
            <div><dt>行情文件目录占用</dt><dd>{{ storageSize(status.worker?.state.data_file_bytes) }}</dd></div>
            <div><dt>容器视角剩余空间</dt><dd>{{ storageSize(status.worker?.state.disk_free_bytes) }}</dd></div>
          </dl>
          <el-alert v-if="status.worker?.state.collection_paused" title="磁盘保护已暂停采集，空间恢复后自动续采" type="error" :closable="false" />
          <el-alert v-else-if="status.worker?.state.disk_alert === 'warning'" title="存储空间接近采集保护阈值" type="warning" :closable="false" />
          <p v-if="status.worker?.state.host_probe_free_bytes != null">宿主盘探针剩余 · {{ storageSize(status.worker.state.host_probe_free_bytes) }}</p>
          <p>项目配额余量 · {{ storageSize(status.worker?.state.quota_remaining_bytes) }}</p>
          <p>最近收到 WS 行情 · {{ dateTime(status.worker?.state.ws_data_at, utc) }}</p>
          <p class="muted">占用每 30 秒更新。数据库占用含表和索引，文件目录含归档和导入文件。</p>
          <p>采集心跳 · {{ status.worker_stale ? '缺失或过期' : '正常' }} · {{ dateTime(status.worker?.heartbeat, utc) }}</p>
          <p>快照采集 · {{ dateTime(status.worker?.state.snapshot_at, utc) }}</p>
          <p>已补齐 K 线至 · {{ dateTime(status.coverage.last_candle, utc) }}</p>
          <p v-if="status.worker?.state.subscription_overflow">超出订阅容量 · {{ status.worker.state.subscription_overflow }} 个资产</p>
          <el-alert v-for="message in [status.worker?.state.snapshot_error, status.worker?.state.history_error, status.worker?.state.funding_error, status.worker?.state.market_cap_error, status.worker?.state.ws_error].filter(Boolean)" :key="message" :title="message" type="warning" :closable="false" />
          <h2>数据缺口</h2>
          <el-table :data="status.gaps" empty-text="暂无记录"><el-table-column prop="data_type" label="来源" /><el-table-column prop="count" label="缺口数" /><el-table-column prop="recent" label="近 24 小时" /></el-table>
        </div>
        <el-empty v-else description="系统状态暂不可用" />
      </section>
    </main>
  </div>
</template>

