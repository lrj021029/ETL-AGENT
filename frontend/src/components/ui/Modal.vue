<!-- 确认对话框（Operator Commit 确认、Schedule 配置等）：Motion 进入/退出动画 + Esc / 遮罩关闭 + 焦点管理 + role="dialog"；接口与原组件一致 -->
<script setup lang="ts">
import { nextTick, onBeforeUnmount, useId, watch } from 'vue'
import { AnimatePresence, motion } from 'motion-v'
import { X } from 'lucide-vue-next'
import { overlayTransition, springs } from '@/lib/motion'

const props = defineProps<{
  open: boolean
  title: string
  description?: string
}>()

const emit = defineEmits<{
  close: []
}>()

const titleId = useId()
const descriptionId = useId()
const panelId = useId()
let restoreFocusTo: HTMLElement | null = null

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  async (isOpen) => {
    if (typeof window === 'undefined') return
    if (isOpen) {
      restoreFocusTo = document.activeElement as HTMLElement | null
      window.addEventListener('keydown', onKeydown)
      document.body.style.overflow = 'hidden'
      await nextTick()
      const panel = document.getElementById(panelId)
      const focusable = panel?.querySelector<HTMLElement>('input, textarea, select, button:not([data-close])')
      ;(focusable ?? panel)?.focus?.()
    } else {
      window.removeEventListener('keydown', onKeydown)
      document.body.style.overflow = ''
      restoreFocusTo?.focus?.()
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})
</script>

<template>
  <Teleport to="body">
    <AnimatePresence>
      <motion.div
        v-if="open"
        key="modal-scrim"
        class="fixed inset-0 z-50 bg-foreground/40"
        :initial="{ opacity: 0 }"
        :animate="{ opacity: 1 }"
        :exit="{ opacity: 0 }"
        :transition="overlayTransition"
        @click="emit('close')"
      />
    </AnimatePresence>
    <AnimatePresence>
      <motion.div
        v-if="open"
        key="modal-panel"
        :id="panelId"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        :aria-describedby="description ? descriptionId : undefined"
        tabindex="-1"
        class="fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-card text-card-foreground shadow-popover outline-none"
        :initial="{ opacity: 0, y: 12, scale: 0.98 }"
        :animate="{ opacity: 1, y: 0, scale: 1 }"
        :exit="{ opacity: 0, y: 8, scale: 0.98 }"
        :transition="springs.gentle"
      >
        <header class="flex items-start justify-between gap-4 border-b px-6 py-4">
          <div>
            <h2 :id="titleId" class="text-[17px] font-semibold">{{ title }}</h2>
            <p v-if="description" :id="descriptionId" class="mt-1 text-[13px] text-muted-foreground">{{ description }}</p>
          </div>
          <button
            type="button"
            data-close
            aria-label="关闭"
            class="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-muted"
            @click="emit('close')"
          >
            <X class="size-4" />
          </button>
        </header>
        <div class="p-6">
          <slot />
        </div>
      </motion.div>
    </AnimatePresence>
  </Teleport>
</template>
