<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type EChartsType } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])
const props = defineProps<{ rows: { variant: string; horizon_s: number; mean_log: number | null;
  matched_gross_return?: number | null; matched_net_return?: number | null }[]; metric?: 'gross' | 'net' | 'prediction' }>()
const target = ref<HTMLElement>()
let chart: EChartsType | undefined
let size: ResizeObserver | undefined

function draw() {
  const variants = [...new Set(props.rows.map(row => row.variant))]
  chart?.setOption({
    animation: false, tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => (Number(value).toFixed(2) + ' bps') },
    legend: { bottom: 0 },
    xAxis: { type: 'value', min: 'dataMin', name: '秒' },
    yAxis: { type: 'value', name: 'bps' },
    grid: { left: 65, right: 50, top: 35, bottom: 55 },
    series: variants.map(variant => ({ name: variant, type: 'line', connectNulls: false,
      data: props.rows.filter(row => row.variant === variant)
        .map(row => {
          const value = props.metric === 'gross' ? row.matched_gross_return : props.metric === 'net' ? row.matched_net_return : row.mean_log
          return [row.horizon_s, value == null ? null : value * 10000]
        }) })),
  }, { notMerge: true })
}
onMounted(() => {
  if (!target.value) return
  chart=init(target.value)
  size=new ResizeObserver(()=>chart?.resize())
  size.observe(target.value)
  draw()
})
watch(()=>[props.rows,props.metric], draw)
onUnmounted(()=>{size?.disconnect();chart?.dispose()})
</script>

<template><div ref="target" class="chart" role="img" aria-label="各消融组未来收益曲线" /></template>

