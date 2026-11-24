<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { init, use, type EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { dateTime } from '../format'

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])
const props = defineProps<{ points: { time: string; equity: string }[]; utc: boolean }>()
const target = ref<HTMLElement>()
let chart: EChartsType | undefined
let resize: ResizeObserver | undefined

function draw() {
  chart?.setOption({
    useUTC: props.utc, animation: false,
    grid: { top: 20, right: 22, bottom: 45, left: 72 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'time', axisLabel: { formatter: (value: number) => dateTime(new Date(value).toISOString(), props.utc) } },
    yAxis: { type: 'value', scale: true, name: 'USDC' },
    series: [{ name: '账户权益', type: 'line', showSymbol: false, sampling: 'lttb',
      data: props.points.map(point => [Date.parse(point.time), Number(point.equity)]), lineStyle: { color: '#087e76' } }],
  })
}
onMounted(() => {
  if (!target.value) return
  chart = init(target.value)
  resize = new ResizeObserver(() => chart?.resize())
  resize.observe(target.value)
  draw()
})
watch(() => [props.points, props.utc], draw)
onUnmounted(() => { resize?.disconnect(); chart?.dispose() })
</script>

<template><div ref="target" class="chart" role="img" aria-label="账户权益曲线" /></template>

