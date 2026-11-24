<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'
import { dateTime, kinds, numeric, selectorNames } from '../format'
import MarketDialog from '../components/MarketDialog.vue'
import type { MarketIdentity, Draft, Market, Member, Pool, Result, Run, Warning } from '../types/models'

defineProps<{ utc: boolean }>()
const types = { MANUAL: '手动', DYNAMIC: '动态', MIXED: '混合' }
const pools = ref<Pool[]>([])
const id = ref('')
const selectedMarket = ref<MarketIdentity>()
const members = ref<Member[]>([])
const runs = ref<Run[]>([])
const results = ref<Result[]>([])
const warnings = ref<Warning[]>([])
const previewed = ref(false)
const busy = ref(false)
const error = ref('')
const candidates = ref<Market[]>([])
const candidate = ref('')
let candidateVersion = 0
const fresh = (): Draft => ({ name: '', type: 'MANUAL', enabled: true, config: {
  selectors: ['volume'], combine: 'UNION', top_n: 10, exit_rank: 15, capacity: 50,
  refresh_seconds: 300, min_hold_seconds: 0, min_volume: 0, max_spread_bps: null,
  market_types: ['PERP'], max_age_seconds: 300,
} })
const draft = ref<Draft>(fresh())
function actionPath() { return '/watchlists/' + id.value }
function payload(): Draft { return { ...draft.value, config: { ...draft.value.config, max_spread_bps: draft.value.config.max_spread_bps ?? null } } }

async function perform(action: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try { await action() } catch (cause) { error.value = (cause as Error).message }
  finally { busy.value = false }
}
async function loadPools() { pools.value = await api<Pool[]>('/watchlists') }
function clearPreview() { results.value = []; warnings.value = []; previewed.value = false }
function newPool() { id.value = ''; draft.value = fresh(); members.value = []; runs.value = []; clearPreview() }
async function open(poolId: string) {
  const path = '/watchlists/' + poolId
  const [detail, history] = await Promise.all([api<Pool & { members: Member[] }>(path), api<Run[]>(path + '/history')])
  id.value = poolId
  const { name, config, type, enabled } = detail
  draft.value = { name, type, enabled, config: { ...config, min_volume: Number(config.min_volume),
    max_spread_bps: config.max_spread_bps == null ? null : Number(config.max_spread_bps) } }
  members.value = detail.members
  runs.value = history
  clearPreview()
}
function save() {
  void perform(async () => {
    const updated = await api<Pool>(id.value ? actionPath() : '/watchlists', id.value ? 'PATCH' : 'POST', payload())
    await loadPools()
    await open(updated.id)
    ElMessage.success('观察池已保存')
  })
}
function evaluate(refresh: boolean) {
  void perform(async () => {
    const outcome = await api<{ results: Result[]; warnings: Warning[] }>((id.value ? actionPath() : '/watchlists') + (refresh ? '/refresh' : '/preview'),
      'POST', refresh ? undefined : payload())
    if (refresh) await open(id.value)
    results.value = outcome.results
    warnings.value = outcome.warnings
    previewed.value = true
  })
}
async function remove() {
  try { await ElMessageBox.confirm('删除此观察池？历史审计将保留。', '删除观察池', { type: 'warning' }) }
  catch { return }
  void perform(async () => { await api(actionPath(), 'DELETE'); await loadPools(); newPool() })
}
async function searchMarket(query: string) {
  const revision = ++candidateVersion
  if (!query) { candidates.value = []; return }
  try {
    const data = await api<{ items: Market[] }>('/markets?limit=25&q=' + encodeURIComponent(query))
    if (revision === candidateVersion) candidates.value = data.items
  } catch (cause) { if (revision === candidateVersion) error.value = (cause as Error).message }
}
function applyMember(instrument: string, state: Pick<Member, 'manual' | 'pinned' | 'blocked'>) {
  void perform(async () => {
    const path = actionPath()
    await api(path + '/members/' + encodeURIComponent(instrument), 'POST',
      { manual: state.manual, pinned: state.pinned, blocked: state.blocked })
    const [detail, history] = await Promise.all([api<Pool & { members: Member[] }>(path), api<Run[]>(path + '/history')])
    members.value = detail.members
    runs.value = history
    candidate.value = ''
  })
}
onMounted(() => void perform(loadPools))
</script>

<template>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <div class="pool-layout" v-loading="busy">
    <section class="panel">
      <div class="toolbar"><el-button type="primary" @click="newPool">新建观察池</el-button></div>
      <el-empty v-if="!pools.length" description="暂无观察池" :image-size="56" />
      <button v-for="pool in pools" :key="pool.id" class="el-button pool-item" :class="{'el-button--primary': id === pool.id}" @click="perform(() => open(pool.id))">
        {{ pool.name }} · {{ types[pool.type] }}
      </button>
    </section>
    <section class="panel">
      <h2>{{ id ? '编辑观察池' : '新建观察池' }}</h2>
      <el-form label-position="top" @submit.prevent="save">
        <div class="form-grid">
          <el-form-item label="名称"><el-input v-model="draft.name" maxlength="100" /></el-form-item>
          <el-form-item label="类型"><el-select v-model="draft.type"><el-option v-for="(label, value) in types" :key="value" :label="label" :value="value" /></el-select></el-form-item>
          <el-form-item label="容量"><el-input-number v-model="draft.config.capacity" :min="1" :max="300" /></el-form-item>
          <el-form-item label="启用"><el-switch v-model="draft.enabled" /></el-form-item>
        </div>
        <div class="form-grid" v-if="draft.type !== 'MANUAL'">
          <el-form-item label="筛选器"><el-select v-model="draft.config.selectors" multiple><el-option v-for="(label, value) in selectorNames" :key="value" :label="label" :value="value" /></el-select></el-form-item>
          <el-form-item label="组合方式"><el-select v-model="draft.config.combine"><el-option label="并集" value="UNION" /><el-option label="交集" value="INTERSECTION" /></el-select></el-form-item>
          <el-form-item label="入池排名（Top N）"><el-input-number v-model="draft.config.top_n" :min="1" :max="200" /></el-form-item>
          <el-form-item label="出池排名"><el-input-number v-model="draft.config.exit_rank" :min="draft.config.top_n" :max="500" /></el-form-item>
          <el-form-item label="刷新周期（秒）"><el-input-number v-model="draft.config.refresh_seconds" :min="30" :max="86400" /></el-form-item>
          <el-form-item label="最短保留（秒）"><el-input-number v-model="draft.config.min_hold_seconds" :min="0" :max="604800" /></el-form-item>
          <el-form-item label="市场类型"><el-select v-model="draft.config.market_types" multiple><el-option v-for="(label, value) in kinds" :key="value" :label="label" :value="value" /></el-select></el-form-item>
          <el-form-item label="最低 24h 成交额（USDC）"><el-input-number v-model="draft.config.min_volume as number" :min="0" /></el-form-item>
          <el-form-item label="最大价差（bps，可空）"><el-input-number v-model="draft.config.max_spread_bps as number | undefined" :min="0" /></el-form-item>
          <el-form-item label="快照最大年龄（秒）"><el-input-number v-model="draft.config.max_age_seconds" :min="60" :max="3600" /></el-form-item>
        </div>
        <div class="toolbar">
          <el-button type="primary" native-type="submit">保存</el-button>
          <el-button v-if="draft.type !== 'MANUAL'" @click="evaluate(false)">预览当前配置</el-button>
          <el-button v-if="id && draft.type !== 'MANUAL'" @click="evaluate(true)">按已保存配置刷新</el-button>
          <el-button v-if="id" type="danger" plain @click="remove">删除</el-button>
        </div>
      </el-form>
      <template v-if="previewed">
        <el-alert v-for="(warning, index) in warnings" :key="index" type="warning" :closable="false"
          :title="warning.selector ? selectorNames[warning.selector] + '数据不可用：' + warning.unavailable + ' / ' + warning.eligible + ' 个候选资产（候选为空时无可用数据）' : '数据不足，保留原有可用成员'" />
        <el-table :data="results" empty-text="无符合条件的资产">
          <el-table-column prop="instrument_id" label="资产 ID" />
          <el-table-column prop="rank" label="排名" /><el-table-column label="分数"><template #default="{ row }">{{ numeric(row.score) }}</template></el-table-column>
          <el-table-column prop="reason" label="筛选依据" />
        </el-table>
      </template>
      <template v-if="id">
        <h2>成员</h2>
        <div class="toolbar">
          <el-select v-model="candidate" filterable remote :remote-method="searchMarket" placeholder="搜索并选择资产" style="width:220px">
            <el-option v-for="item in candidates" :key="item.instrument_id" :label="item.symbol + ' · ' + kinds[item.market_type]" :value="item.instrument_id" />
          </el-select>
          <el-button :disabled="!candidate" @click="applyMember(candidate, {manual:true,pinned:false,blocked:false})">加入</el-button>
          <el-button :disabled="!candidate" @click="applyMember(candidate, {manual:false,pinned:false,blocked:true})">加入黑名单</el-button>
        </div>
        <el-table :data="members" empty-text="暂无成员">
          <el-table-column prop="symbol" label="交易对" min-width="125">
            <template #default="{ row }"><el-button link type="primary" @click="selectedMarket = row">{{ row.symbol }}</el-button></template>
          </el-table-column>
          <el-table-column label="来源" min-width="130"><template #default="{row}">{{ [row.manual && '手动', row.automatic && '自动', row.pinned && '置顶', row.blocked && '黑名单', row.status === 'DELISTED' && '已下线'].filter(Boolean).join(' / ') }}</template></el-table-column>
          <el-table-column label="操作" min-width="205"><template #default="{row}">
            <el-button link @click="applyMember(row.instrument_id, {...row,pinned:!row.pinned})">{{ row.pinned ? '取消置顶' : '置顶' }}</el-button>
            <el-button link @click="applyMember(row.instrument_id, {...row,blocked:!row.blocked})">{{ row.blocked ? '移出黑名单' : '拉黑' }}</el-button>
            <el-button v-if="row.manual" link @click="applyMember(row.instrument_id, {...row,manual:false,pinned:false})">移出手动来源</el-button>
          </template></el-table-column>
        </el-table>
        <div class="history"><h2>进出池记录</h2>
          <el-empty v-if="!runs.length" description="暂无筛选记录" :image-size="56" />
          <el-collapse v-else>
            <el-collapse-item v-for="run in runs" :key="run.id" :title="dateTime(run.as_of, utc) + ' · 规则 v' + run.version + ' · ' + run.results.length + ' 条记录'">
              <el-table :data="run.results" empty-text="此次刷新无成员变更">
                <el-table-column prop="symbol" label="资产" /><el-table-column prop="action" label="动作" />
                <el-table-column prop="rank" label="排名" /><el-table-column prop="reason" label="原因" />
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </div>
      </template>
    </section>
  </div>
  <MarketDialog :instrument="selectedMarket" :utc="utc" @close="selectedMarket = undefined" />
</template>
