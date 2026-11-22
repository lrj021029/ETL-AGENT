<!-- 显式六态视图容器：Loading / Empty / Error / Stale / Partial / Success -->
<script setup lang="ts">
import type { ViewDataState } from '@/types/api'

defineProps<{
  state: ViewDataState
  errorMessage?: string | null
  errorCode?: string | null
  traceId?: string | null
  emptyTitle?: string
  emptyDescription?: string
  staleNotice?: string
  partialNotice?: string
}>()

defineEmits<{
  retry: []
}>()
</script>

<template>
  <div data-testid="state-view" :data-state="state">
    <!-- Loading 状态 -->
    <div
      v-if="state === 'loading'"
      class="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed bg-card/50 p-10 text-sm text-muted-foreground"
    >
      <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <span>正在从控制面加载实时数据…</span>
    </div>

    <!-- Error 状态（保留后端 error_code 与 trace_id） -->
    <div
      v-else-if="state === 'error'"
      role="alert"
      class="rounded-lg border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
    >
      <div class="flex items-start justify-between gap-4">
        <div class="space-y-1">
          <div class="font-semibold text-rose-300">
            请求失败<span v-if="errorCode">（{{ errorCode }}）</span>
          </div>
          <p class="text-xs text-rose-200/90">{{ errorMessage || '控制面返回异常响应' }}</p>
          <p v-if="traceId" class="font-mono text-[11px] text-rose-300/80">
            Trace ID: {{ traceId }}
          </p>
        </div>
        <button
          type="button"
          class="rounded-md border border-rose-400/40 bg-rose-500/20 px-3 py-1.5 text-xs font-medium text-rose-100 hover:bg-rose-500/30"
          @click="$emit('retry')"
        >
          重试
        </button>
      </div>
    </div>

    <!-- Empty 状态 -->
    <div
      v-else-if="state === 'empty'"
      class="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed bg-card/40 p-10 text-center"
    >
      <p class="text-sm font-medium">{{ emptyTitle || '暂无数据' }}</p>
      <p class="max-w-md text-xs text-muted-foreground">
        {{ emptyDescription || '当前项目上下文下尚无匹配记录。' }}
      </p>
      <slot name="empty-action" />
    </div>

    <!-- Stale / Partial / Success 均渲染主内容，并在 Stale 或 Partial 时附带明确提示条 -->
    <div v-else class="space-y-3">
      <div
        v-if="state === 'stale'"
        class="flex items-center justify-between rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
      >
        <span>{{ staleNotice || '数据可能已过期（心跳超时或缓存陈旧），建议刷新获取最新状态。' }}</span>
        <button
          type="button"
          class="rounded border border-amber-400/40 px-2 py-0.5 text-xs hover:bg-amber-500/20"
          @click="$emit('retry')"
        >
          立即刷新
        </button>
      </div>

      <div
        v-if="state === 'partial'"
        class="rounded-md border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-xs text-sky-300"
      >
        {{ partialNotice || '部分子模块数据暂未就绪，当前仅展示已成功返回的控制面事实。' }}
      </div>

      <slot />
    </div>
  </div>
</template>
