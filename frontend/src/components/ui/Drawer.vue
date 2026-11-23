<!-- 右侧抽屉（连接器能力矩阵、Preparation 冻结事实等）：Motion 弹簧滑入/滑出 + Esc / 遮罩关闭 + 焦点管理 + role="dialog"；接口与原组件一致 -->
<script setup lang="ts">
import { nextTick, onBeforeUnmount, useId, watch } from 'vue'
import { AnimatePresence, motion } from 'motion-v'
import { overlayTransition, springs } from '@/lib/motion'

const props = defineProps<{
  open: boolean
  title: string
  subtitle?: string
}>()

const emit = defineEmits<{
  close: []
}>()

const titleId = useId()
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
      document.getElementById(panelId)?.focus()
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
        key="drawer-scrim"
        class="fixed inset-0 z-50 bg-foreground/40"
        :initial="{ opacity: 0 }"
        :animate="{ opacity: 1 }"
        :exit="{ opacity: 0 }"
        :transition="overlayTransition"
        @click="emit('close')"
      />
    </AnimatePresence>
    <AnimatePresence>
      <motion.aside
        v-if="open"
        key="drawer-panel"
        :id="panelId"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        tabindex="-1"
        class="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l bg-card text-card-foreground shadow-popover outline-none sm:rounded-l-xl"
        :initial="{ x: 48, opacity: 0 }"
        :animate="{ x: 0, opacity: 1 }"
        :exit="{ x: 48, opacity: 0 }"
        :transition="springs.gentle"
      >
        <header class="flex items-center justify-between gap-4 border-b px-6 py-4">
          <div class="min-w-0">
            <h2 :id="titleId" class="truncate text-[17px] font-semibold">{{ title }}</h2>
            <p v-if="subtitle" class="truncate text-[13px] text-muted-foreground">{{ subtitle }}</p>
          </div>
          <button
            type="button"
            class="h-8 shrink-0 rounded-md border px-3 text-[13px] hover:bg-muted"
            @click="emit('close')"
          >
            关闭
          </button>
        </header>
        <div class="flex-1 overflow-y-auto p-6">
          <slot />
        </div>
      </motion.aside>
    </AnimatePresence>
  </Teleport>
</template>
