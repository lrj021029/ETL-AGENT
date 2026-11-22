<!-- 确认对话框组件（用于 Operator Commit 确认、Schedule 配置等高危受管操作） -->
<script setup lang="ts">
defineProps<{
  open: boolean
  title: string
  description?: string
}>()

defineEmits<{
  close: []
}>()
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
      <div class="w-full max-w-lg rounded-lg border bg-card text-card-foreground shadow-2xl">
        <header class="border-b px-5 py-4">
          <h2 class="text-base font-semibold">{{ title }}</h2>
          <p v-if="description" class="mt-1 text-xs text-muted-foreground">{{ description }}</p>
        </header>
        <div class="p-5">
          <slot />
        </div>
      </div>
    </div>
  </Teleport>
</template>
