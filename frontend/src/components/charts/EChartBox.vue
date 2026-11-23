<!-- Apache ECharts 响应式图表封装：颜色读取 CSS 设计令牌（浅色主题），保留原 option / height 接口 -->
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

function token(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

/** 主题基底：调色板、文字与坐标轴颜色来自设计令牌，业务 option 中的显式设置优先 */
function withTheme(option: EChartsOption): EChartsOption {
  const muted = token('--muted-foreground', '#667085')
  const border = token('--border', '#e5e7eb')
  return {
    color: ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5'].map((v, i) =>
      token(v, ['#635bdb', '#98a2b3', '#16a36a', '#d99018', '#d64545'][i]),
    ),
    textStyle: { color: muted, fontFamily: token('--font-sans', 'Segoe UI, sans-serif') },
    grid: { left: 48, right: 48, top: 36, bottom: 32, containLabel: true },
    tooltip: { backgroundColor: '#ffffff', borderColor: border, textStyle: { color: token('--foreground', '#202938') } },
    ...option,
  }
}

function initChart(): void {
  if (!containerRef.value) return
  chartInstance = echarts.init(containerRef.value)
  chartInstance.setOption(withTheme(props.option))
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
    chartInstance?.setOption(withTheme(nextOption), true)
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
