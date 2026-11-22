<!-- Pipeline Studio DAG 拓扑可视化组件（基于 @vue-flow/core） -->
<script setup lang="ts">
import { computed } from 'vue'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import type { PipelineDraftArtifacts } from '@/types/api'

const props = defineProps<{
  nodes: PipelineDraftArtifacts['dag_nodes']
  edges: PipelineDraftArtifacts['dag_edges']
}>()

const flowNodes = computed(() =>
  props.nodes.map((n, index) => ({
    id: n.id,
    position: { x: 80 + index * 240, y: n.kind === 'TRANSFORM' ? 110 : 80 },
    data: {
      label: `[${n.kind}] ${n.label}${n.execution_location ? ` (${n.execution_location})` : ''}`,
    },
    style: {
      borderRadius: '8px',
      padding: '10px 14px',
      fontSize: '12px',
      fontWeight: 600,
      border:
        n.kind === 'SOURCE'
          ? '1px solid #38bdf8'
          : n.kind === 'TRANSFORM'
            ? '1px solid #a78bfa'
            : '1px solid #34d399',
      background: '#0f172a',
      color: '#f8fafc',
    },
  })),
)

const flowEdges = computed(() =>
  props.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label,
    animated: true,
  })),
)
</script>

<template>
  <div class="h-80 w-full overflow-hidden rounded-md border bg-slate-950">
    <div
      v-if="nodes.length === 0"
      class="flex h-full items-center justify-center text-xs text-muted-foreground"
    >
      暂无 DAG 拓扑节点，请先在左侧输入需求并运行 LangGraph 生成候选方案。
    </div>
    <VueFlow v-else :nodes="flowNodes" :edges="flowEdges" fit-view-on-init>
      <Background />
      <Controls />
    </VueFlow>
  </div>
</template>
