<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import StrategyFields from '../components/StrategyFields.vue'
import type { Parameters, StrategyDefinition } from '../types/backtest'

const emit = defineEmits<{ backtest: [string, Parameters] }>()
const definitions = ref<StrategyDefinition[]>([])
const selected = ref('')
const instances = ref<{ id: string; strategy_id: string; status: string; runtime_status: string | null }[]>([])
const params = ref<Parameters>({})
const error = ref('')
const definition = computed(() => definitions.value.find(item => item.id === selected.value))
watch(definition, item => { if (item) params.value = { ...item.defaults } })
onMounted(async () => {
  try {
    [definitions.value, instances.value] = await Promise.all([api<StrategyDefinition[]>('/strategies'), api<typeof instances.value>('/strategy-instances')])
    selected.value = definitions.value[0]?.id ?? ''
  } catch (cause) { error.value = (cause as Error).message }
})
</script>

<template>
  <section class="panel">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-select v-model="selected" aria-label="已注册策略" style="width:270px">
      <el-option v-for="item in definitions" :key="item.id" :value="item.id" :label="item.name" />
    </el-select>
    <template v-if="definition">
      <p class="muted">版本 {{ definition.version }} · 研究与框架验证</p>
      <el-form label-position="top"><StrategyFields v-model="params" :definition="definition" /></el-form>
      <el-button type="primary" @click="emit('backtest', selected, params)">使用此配置回测</el-button>
    </template>
  </section>
  <section class="panel">
    <h2>回测策略实例</h2>
    <el-table :data="instances" empty-text="暂无实例">
      <el-table-column prop="id" label="实例 ID" min-width="260" />
      <el-table-column prop="strategy_id" label="策略" min-width="140" />
      <el-table-column prop="status" label="任务状态" />
      <el-table-column prop="runtime_status" label="运行时状态" />
    </el-table>
  </section>
</template>

