<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { dateTime, kinds, numeric, percent } from '../format'
import MarketDialog from '../components/MarketDialog.vue'
import type { Kind, Market, Pool } from '../types/models'

defineProps<{ utc: boolean }>()
const search = ref('')
const kind = ref<Kind | ''>(''); const page = ref(1); const total = ref(0)
const items = ref<Market[]>([]); const pools = ref<Pool[]>([]); const pool = ref('')
const error = ref(''); const loading = ref(false)
const sort = ref('volume_24h'); const direction = ref('desc')
const selected = ref<Market>()
let timer: ReturnType<typeof setInterval>
let queryVersion = 0

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
watch([search, kind], () => { if (page.value !== 1) page.value = 1; else void load() })
watch(page, () => void load())
onMounted(async () => {
  void load()
  timer = setInterval(() => void load(true), 15_000)
  try { pools.value = await api<Pool[]>('/watchlists') } catch (cause) { ElMessage.error((cause as Error).message) }
})
onUnmounted(() => { clearInterval(timer) })
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
        <template #default="{ row }"><el-button link type="primary" @click="selected = row">{{ row.symbol }}</el-button>
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
  <MarketDialog :instrument="selected" :utc="utc" @close="selected = undefined" />
</template>
