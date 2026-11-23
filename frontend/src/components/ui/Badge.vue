<!-- 语义化状态徽章组件（浅色令牌）：soft 底 + *-text 文字，保证小字号对比度；接口与原组件一致 -->
<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'outline'
  }>(),
  { variant: 'default' },
)

const variantClass = computed(() => {
  switch (props.variant) {
    case 'success':
      return 'bg-success-soft text-success-text border-success/25'
    case 'warning':
      return 'bg-warning-soft text-warning-text border-warning/30'
    case 'danger':
      return 'bg-destructive-soft text-destructive-text border-destructive/25'
    case 'info':
      return 'bg-info-soft text-info-text border-info/25'
    case 'outline':
      return 'bg-transparent text-muted-foreground border-border'
    default:
      return 'bg-muted text-foreground/80 border-border'
  }
})
</script>

<template>
  <span
    class="inline-flex h-[22px] items-center gap-1 rounded-md border px-2 text-xs font-medium whitespace-nowrap"
    :class="variantClass"
  >
    <slot />
  </span>
</template>
