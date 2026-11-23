<!-- Hover / Press 交互包装：悬停轻微上浮、按下弹簧缩放；用于按钮、可点击卡片 -->
<script setup lang="ts">
import { motion } from 'motion-v'
import { springs } from '@/lib/motion'

const props = withDefaults(defineProps<{ lift?: number; pressScale?: number; block?: boolean }>(), {
  lift: -1,
  pressScale: 0.97,
  block: false,
})
</script>

<template>
  <motion.div
    :class="props.block ? 'block' : 'inline-flex'"
    :while-hover="{ y: props.lift }"
    :while-press="{ scale: props.pressScale }"
    :transition="springs.snappy"
  >
    <slot />
  </motion.div>
</template>
