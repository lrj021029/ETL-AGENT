<!-- 语义化状态徽章组件 -->
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
      return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
    case 'warning':
      return 'bg-amber-500/15 text-amber-400 border-amber-500/30'
    case 'danger':
      return 'bg-rose-500/15 text-rose-400 border-rose-500/30'
    case 'info':
      return 'bg-sky-500/15 text-sky-400 border-sky-500/30'
    case 'outline':
      return 'bg-transparent text-muted-foreground border-border'
    default:
      return 'bg-secondary text-secondary-foreground border-border'
  }
})
</script>

<template>
  <span
    class="inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium tracking-tight"
    :class="variantClass"
  >
    <slot />
  </span>
</template>
