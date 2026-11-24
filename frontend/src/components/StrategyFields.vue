<script setup lang="ts">
import type { Parameters, StrategyDefinition } from '../types/backtest'
const props = defineProps<{ definition: StrategyDefinition }>()
const values = defineModel<Parameters>({ required: true })
const labels: Record<string, string> = {
  timeframe: 'K 线周期', fast_ema: '快 EMA', slow_ema: '慢 EMA', take_profit: '止盈比例（0.03 = 3%）',
  exit_mode: '止盈止损模式', trailing_stop: '回撤比例（0.05 = 5%）',
  stop_loss: '止损比例（0.015 = 1.5%）', position_notional: '名义金额（USDC）',
  initial_balance: '初始资金（USDC）', leverage: '杠杆', direction: '交易方向',
}
const modeLabels: Record<string, string> = { FIXED: '固定止盈止损', TRAILING: '移动止损 + 固定止损' }
function showField(key: string) {
  if (key === 'take_profit') return values.value.exit_mode !== 'TRAILING'
  if (key === 'trailing_stop') return values.value.exit_mode === 'TRAILING'
  return true
}
function update(key: string, value: string | number | undefined) {
  if (value !== undefined) values.value = { ...values.value, [key]: value }
}
</script>

<template>
  <div class="form-grid">
    <el-form-item v-for="key in Object.keys(props.definition.defaults).filter(showField)" :key="key" :label="labels[key] ?? key">
      <el-select v-if="props.definition.parameter_schema.properties[key]?.enum" :model-value="values[key]" @update:model-value="update(key, $event)">
        <el-option v-for="option in props.definition.parameter_schema.properties[key].enum" :key="option" :value="option" :label="modeLabels[option] ?? option" />
      </el-select>
      <el-input-number v-else :model-value="Number(values[key])"
        :min="props.definition.parameter_schema.properties[key]?.minimum"
        :max="props.definition.parameter_schema.properties[key]?.maximum"
        :step="['take_profit', 'stop_loss', 'trailing_stop'].includes(key) ? 0.001 : 1" @update:model-value="update(key, $event)" />
    </el-form-item>
  </div>
</template>

