<!-- Monaco Editor / Diff Editor 封装：浅色主题 vs；外部 modelValue 变化同步到已挂载的编辑器（修复 Agent 返回新草稿后内容不刷新）。接口不变 -->
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
let syncModified: ((value: string) => void) | null = null
let syncOriginal: ((value: string) => void) | null = null

const editorOptions = {
  automaticLayout: true,
  theme: 'vs',
  minimap: { enabled: false },
  fontSize: 13,
  fontFamily: '"JetBrains Mono", "Cascadia Mono", Consolas, monospace',
  scrollBeyondLastLine: false,
  renderLineHighlight: 'line' as const,
  padding: { top: 10, bottom: 10 },
}

async function mountMonaco(): Promise<void> {
  if (!editorContainer.value) return
  try {
    const monaco = await import('monaco-editor')
    if (props.diffMode) {
      const diffEditor = monaco.editor.createDiffEditor(editorContainer.value, {
        ...editorOptions,
        readOnly: props.readOnly,
      })
      const originalModel = monaco.editor.createModel(props.originalValue, props.language)
      const modifiedModel = monaco.editor.createModel(props.modelValue, props.language)
      diffEditor.setModel({ original: originalModel, modified: modifiedModel })
      syncModified = (value) => {
        if (modifiedModel.getValue() !== value) modifiedModel.setValue(value)
      }
      syncOriginal = (value) => {
        if (originalModel.getValue() !== value) originalModel.setValue(value)
      }
      disposeHandle = () => {
        diffEditor.dispose()
        originalModel.dispose()
        modifiedModel.dispose()
      }
    } else {
      const editor = monaco.editor.create(editorContainer.value, {
        ...editorOptions,
        value: props.modelValue,
        language: props.language,
        readOnly: props.readOnly,
      })
      const sub = editor.onDidChangeModelContent(() => {
        emit('update:modelValue', editor.getValue())
      })
      syncModified = (value) => {
        if (editor.getValue() !== value) editor.setValue(value)
      }
      syncOriginal = null
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
    syncModified = null
    syncOriginal = null
    if (!fallbackMode.value) {
      void mountMonaco()
    }
  },
)

watch(
  () => props.modelValue,
  (value) => syncModified?.(value),
)
watch(
  () => props.originalValue,
  (value) => syncOriginal?.(value),
)

onBeforeUnmount(() => {
  disposeHandle?.()
  disposeHandle = null
})
</script>

<template>
  <div class="overflow-hidden rounded-lg border bg-card">
    <div v-if="!fallbackMode" ref="editorContainer" class="w-full" :style="{ height }" />
    <div v-else class="grid gap-2 p-3" :class="diffMode ? 'grid-cols-2' : 'grid-cols-1'">
      <textarea
        v-if="diffMode"
        :value="originalValue"
        readonly
        aria-label="原始内容"
        class="h-64 w-full rounded-md border bg-muted p-2 font-mono text-xs text-foreground"
      />
      <textarea
        :value="modelValue"
        :readonly="readOnly"
        aria-label="编辑内容"
        class="h-64 w-full rounded-md border bg-card p-2 font-mono text-xs text-foreground"
        @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
      />
    </div>
  </div>
</template>
