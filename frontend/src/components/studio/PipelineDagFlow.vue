<!-- Pipeline Studio DAG 拓扑可视化（@vue-flow/core）：浅色节点，左侧色条区分 SOURCE / TRANSFORM / SINK；
     连线默认静态，只对选中节点相关的边做动画，避免大规模节点持续动画。接口不变，新增可选 select 事件 -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import type { PipelineDraftArtifacts } from '@/types/api'

const props = defineProps<{
  nodes: PipelineDraftArtifacts['dag_nodes']
  edges: PipelineDraftArtifacts['dag_edges']
}>()

const emit = defineEmits<{
  select: [nodeId: string | null]
}>()

const selectedId = ref<string | null>(null)

const kindColor: Record<string, string> = {
  SOURCE: 'var(--primary)',
  TRANSFORM: '#98a2b3',
  SINK: 'var(--success)',
}

const flowNodes = computed(() =>
  props.nodes.map((n, index) => ({
    id: n.id,
    position: { x: 60 + index * 260, y: n.kind === 'TRANSFORM' ? 120 : 80 },
    data: {
      label: `${n.label}${n.execution_location ? ` · ${n.execution_location}` : ''}`,
    },
    style: {
      width: '200px',
      borderRadius: '10px',
      padding: '12px 14px 12px 16px',
      fontSize: '13px',
      fontWeight: 600,
      textAlign: 'left' as const,
      color: 'var(--foreground)',
      background: 'var(--card)',
      border: selectedId.value === n.id ? '2px solid var(--primary)' : '1px solid var(--border)',
      borderLeft: `4px solid ${kindColor[n.kind] ?? '#98a2b3'}`,
      boxShadow: '0 1px 2px rgb(16 24 40 / 0.05)',
    },
  })),
)

const flowEdges = computed(() =>
  props.edges.map((e) => {
    const related = selectedId.value !== null && (e.source === selectedId.value || e.target === selectedId.value)
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.label,
      animated: related,
      style: { stroke: related ? 'var(--primary)' : '#b9bfc9', strokeWidth: related ? 2 : 1.5 },
      labelStyle: { fill: 'var(--muted-foreground)', fontSize: '11px' },
    }
  }),
)

function onNodeClick(payload: { node: { id: string } }): void {
  selectedId.value = selectedId.value === payload.node.id ? null : payload.node.id
  emit('select', selectedId.value)
}

function onPaneClick(): void {
  selectedId.value = null
  emit('select', null)
}
</script>

<template>
  <div class="h-80 w-full overflow-hidden rounded-lg border bg-[#fbfbfd]">
    <div
      v-if="nodes.length === 0"
      class="flex h-full items-center justify-center px-6 text-center text-[13px] text-muted-foreground"
    >
      暂无 DAG 拓扑节点，请先在左侧输入需求并运行 LangGraph 生成候选方案。
    </div>
    <VueFlow
      v-else
      :nodes="flowNodes"
      :edges="flowEdges"
      fit-view-on-init
      @node-click="onNodeClick"
      @pane-click="onPaneClick"
    >
      <Background pattern-color="#dadde3" :gap="16" />
      <Controls position="bottom-left" />
    </VueFlow>
  </div>
</template>
