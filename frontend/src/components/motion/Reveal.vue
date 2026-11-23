<!-- 滚动触发入场动画：元素首次进入视口时淡入上移，只播放一次；减少动态效果时由 MotionConfig 关闭位移 -->
<script setup lang="ts">
import { motion } from 'motion-v'
import { easeOut, revealInView } from '@/lib/motion'

const props = withDefaults(defineProps<{ delay?: number; offset?: number }>(), {
  delay: 0,
  offset: 12,
})
</script>

<template>
  <motion.div
    :initial="{ opacity: 0, y: props.offset }"
    :while-in-view="{ opacity: 1, y: 0 }"
    :in-view-options="revealInView"
    :transition="{ duration: 0.35, ease: easeOut, delay: props.delay }"
  >
    <slot />
  </motion.div>
</template>
