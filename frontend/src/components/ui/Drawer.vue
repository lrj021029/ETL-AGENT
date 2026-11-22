<!-- 侧边抽屉组件（用于连接器能力矩阵、Preparation 冻结事实详情等） -->
<script setup lang="ts">
defineProps<{
  open: boolean
  title: string
  subtitle?: string
}>()

defineEmits<{
  close: []
}>()
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm">
      <div class="flex h-full w-full max-w-2xl flex-col border-l bg-card text-card-foreground shadow-2xl">
        <header class="flex items-center justify-between border-b px-5 py-4">
          <div>
            <h2 class="text-base font-semibold">{{ title }}</h2>
            <p v-if="subtitle" class="text-xs text-muted-foreground">{{ subtitle }}</p>
          </div>
          <button
            type="button"
            class="rounded-md border px-2.5 py-1 text-xs hover:bg-accent"
            @click="$emit('close')"
          >
            关闭
          </button>
        </header>
        <div class="flex-1 overflow-y-auto p-5">
          <slot />
        </div>
      </div>
    </div>
  </Teleport>
</template>
