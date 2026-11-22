<!-- Monaco Editor / Diff Editor 封装组件，用于 SQL / HOCON 编辑与 Schema/HOCON Diff 审查 -->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: string
    originalValue?: string
    language?: 'sql' | 'json' | 'ini' | 'plaintext'
    diffMode?: boolean
    readOnly?: boolean
    height?: string
  }>(),
  {
    originalValue: '',
    language: 'sql',
    diffMode: false,
    readOnly: false,
    height: '320px',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const editorContainer = ref<HTMLDivElement | null>(null)
const fallbackMode = ref(false)
let disposeHandle: (() => void) | null = null

async function mountMonaco(): Promise<void> {
  if (!editorContainer.value) return
  try {
    const monaco = await import('monaco-editor')
    if (props.diffMode) {
      const diffEditor = monaco.editor.createDiffEditor(editorContainer.value, {
        readOnly: props.readOnly,
        automaticLayout: true,
        theme: 'vs-dark',
        minimap: { enabled: false },
        fontSize: 12,
      })
      const originalModel = monaco.editor.createModel(props.originalValue, props.language)
      const modifiedModel = monaco.editor.createModel(props.modelValue, props.language)
      diffEditor.setModel({ original: originalModel, modified: modifiedModel })
      disposeHandle = () => {
        diffEditor.dispose()
        originalModel.dispose()
        modifiedModel.dispose()
      }
    } else {
      const editor = monaco.editor.create(editorContainer.value, {
        value: props.modelValue,
        language: props.language,
        readOnly: props.readOnly,
        automaticLayout: true,
        theme: 'vs-dark',
        minimap: { enabled: false },
        fontSize: 12,
      })
      const sub = editor.onDidChangeModelContent(() => {
        emit('update:modelValue', editor.getValue())
      })
      disposeHandle = () => {
        sub.dispose()
        editor.dispose()
      }
    }
  } catch {
    // 测试环境或 Worker 受限时降级为等宽文本域，保证编辑与审查功能可用
    fallbackMode.value = true
  }
}

onMounted(() => {
  void mountMonaco()
})

watch(
  () => [props.diffMode, props.language, props.readOnly],
  () => {
    disposeHandle?.()
    disposeHandle = null
    if (!fallbackMode.value) {
      void mountMonaco()
    }
  },
)

onBeforeUnmount(() => {
  disposeHandle?.()
  disposeHandle = null
})
</script>

<template>
  <div class="overflow-hidden rounded-md border bg-zinc-950">
    <div v-if="!fallbackMode" ref="editorContainer" class="w-full" :style="{ height }" />
    <div v-else class="grid gap-2 p-3" :class="diffMode ? 'grid-cols-2' : 'grid-cols-1'">
      <textarea
        v-if="diffMode"
        :value="originalValue"
        readonly
        class="h-64 w-full rounded border bg-zinc-900 p-2 font-mono text-xs text-zinc-300"
      />
      <textarea
        :value="modelValue"
        :readonly="readOnly"
        class="h-64 w-full rounded border bg-zinc-900 p-2 font-mono text-xs text-zinc-100"
        @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
      />
    </div>
  </div>
</template>
