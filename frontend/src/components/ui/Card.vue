<!-- 控制台卡片容器：白底 + 1px 边框，不使用重阴影；接口与原组件一致 -->
<script setup lang="ts">
defineProps<{
  title?: string
  subtitle?: string
}>()
</script>

<template>
  <section
    class="min-w-0 overflow-hidden rounded-[10px] border bg-card text-card-foreground transition-[border-color,box-shadow] duration-200 hover:border-primary/25 hover:shadow-[0_2px_10px_-4px_rgb(16_24_40_/_0.08)]"
  >
    <header
      v-if="title || $slots.header"
      class="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3.5"
    >
      <div class="min-w-0">
        <h3 v-if="title" class="text-[15px] leading-[22px] font-semibold">{{ title }}</h3>
        <p v-if="subtitle" class="text-xs text-muted-foreground">{{ subtitle }}</p>
      </div>
      <slot name="header" />
    </header>
    <div class="p-5">
      <slot />
    </div>
  </section>
</template>
