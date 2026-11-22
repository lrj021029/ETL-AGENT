<!-- 企业控制台卡片容器 -->
<script setup lang="ts">
defineProps<{
  title?: string
  subtitle?: string
}>()
</script>

<template>
  <section class="rounded-lg border bg-card text-card-foreground shadow-sm">
    <header
      v-if="title || $slots.header"
      class="flex items-center justify-between gap-3 border-b px-4 py-3"
    >
      <div>
        <h3 v-if="title" class="text-sm font-semibold tracking-tight">{{ title }}</h3>
        <p v-if="subtitle" class="text-xs text-muted-foreground">{{ subtitle }}</p>
      </div>
      <slot name="header" />
    </header>
    <div class="p-4">
      <slot />
    </div>
  </section>
</template>
