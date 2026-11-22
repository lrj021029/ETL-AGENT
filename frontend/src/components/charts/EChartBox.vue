<!-- Apache ECharts 响应式图表封装组件，用于实时吞吐量、拒绝率与 Benchmark 趋势对比 -->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'

const props = withDefaults(
  defineProps<{
    option: EChartsOption
    height?: string
  }>(),
  { height: '260px' },
)

const containerRef = ref<HTMLDivElement | null>(null)
let chartInstance: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null

function initChart(): void {
  if (!containerRef.value) return
  chartInstance = echarts.init(containerRef.value)
  chartInstance.setOption(props.option)
}

onMounted(() => {
  try {
    initChart()
    if (typeof ResizeObserver !== 'undefined' && containerRef.value) {
      resizeObserver = new ResizeObserver(() => {
        chartInstance?.resize()
      })
      resizeObserver.observe(containerRef.value)
    }
  } catch {
    // 无 Canvas 环境（如 jsdom 单元测试）下静默降级
  }
})

watch(
  () => props.option,
  (nextOption) => {
    chartInstance?.setOption(nextOption, true)
  },
  { deep: true },
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chartInstance?.dispose()
  chartInstance = null
})
</script>

<template>
  <div ref="containerRef" class="w-full" :style="{ height }" />
</template>
