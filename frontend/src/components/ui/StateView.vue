<!-- 显式六态视图容器：Loading / Empty / Error / Stale / Partial / Success（浅色令牌；文案、data-testid 与事件保持不变） -->
<script setup lang="ts">
import { AlertTriangle, Info, RefreshCw } from 'lucide-vue-next'
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
    <Transition name="state-fade" mode="out-in">
      <!-- Loading：微光骨架屏保留布局高度 -->
      <div
        v-if="state === 'loading'"
        key="loading"
        class="space-y-3 rounded-[10px] border bg-card p-5"
        aria-busy="true"
      >
        <div class="skeleton-shimmer h-4 w-40 rounded" />
        <div class="grid gap-3 sm:grid-cols-3">
          <div class="skeleton-shimmer h-20 rounded-lg" />
          <div class="skeleton-shimmer h-20 rounded-lg" />
          <div class="skeleton-shimmer h-20 rounded-lg" />
        </div>
        <p class="text-xs text-muted-foreground">正在从控制面加载实时数据…</p>
      </div>

      <!-- Error（保留后端 error_code 与 trace_id） -->
      <div
        v-else-if="state === 'error'"
        key="error"
        role="alert"
        class="rounded-[10px] border border-destructive/30 bg-destructive-soft p-4 text-sm text-destructive-text"
      >
        <div class="flex items-start justify-between gap-4">
          <div class="flex gap-3">
            <AlertTriangle class="mt-0.5 size-4 shrink-0" />
            <div class="space-y-1">
              <div class="font-semibold">
                请求失败<span v-if="errorCode">（{{ errorCode }}）</span>
              </div>
              <p class="text-[13px]">{{ errorMessage || '控制面返回异常响应' }}</p>
              <p v-if="traceId" class="font-mono text-xs opacity-80 select-all">Trace ID: {{ traceId }}</p>
            </div>
          </div>
          <button
            type="button"
            class="inline-flex h-8 items-center gap-1.5 rounded-md border border-destructive/30 bg-card px-3 text-xs font-medium text-destructive-text transition-all duration-150 hover:bg-destructive-soft active:scale-[0.97]"
            @click="$emit('retry')"
          >
            <RefreshCw class="size-3.5" />
            重试
          </button>
        </div>
      </div>

      <!-- Empty -->
      <div
        v-else-if="state === 'empty'"
        key="empty"
        class="flex flex-col items-center justify-center gap-2 rounded-[10px] border border-dashed bg-card p-10 text-center"
      >
        <div class="grid size-9 place-items-center rounded-lg bg-muted text-muted-foreground">
          <Info class="size-4" />
        </div>
        <p class="text-sm font-semibold">{{ emptyTitle || '暂无数据' }}</p>
        <p class="max-w-md text-[13px] text-muted-foreground">
          {{ emptyDescription || '当前项目上下文下尚无匹配记录。' }}
        </p>
        <slot name="empty-action" />
      </div>

      <!-- Stale / Partial / Success 均渲染主内容，并在 Stale 或 Partial 时附带明确提示条 -->
      <div v-else key="content" class="space-y-3">
        <div
          v-if="state === 'stale'"
          role="status"
          class="flex items-center justify-between gap-3 rounded-lg border border-warning/30 bg-warning-soft px-3.5 py-2.5 text-[13px] text-warning-text"
        >
          <span>{{ staleNotice || '数据可能已过期（心跳超时或缓存陈旧），建议刷新获取最新状态。' }}</span>
          <button
            type="button"
            class="h-7 shrink-0 rounded-md border border-warning/40 bg-card px-2.5 text-xs font-medium transition-transform active:scale-[0.97] hover:bg-warning-soft"
            @click="$emit('retry')"
          >
            立即刷新
          </button>
        </div>

        <div
          v-if="state === 'partial'"
          role="status"
          class="rounded-lg border border-info/25 bg-info-soft px-3.5 py-2.5 text-[13px] text-info-text"
        >
          {{ partialNotice || '部分子模块数据暂未就绪，当前仅展示已成功返回的控制面事实。' }}
        </div>

        <slot />
      </div>
    </Transition>
  </div>
</template>
