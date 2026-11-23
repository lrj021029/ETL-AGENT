<!-- 运行中心与质量诊断：Batch/CDC 分模式状态机、RealtimeStreamAdapter 实时流、confirmation_state 显式指示、ECharts 指标与只读诊断建议 -->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { api, apiClient } from '@/api'
import { ApiClientError } from '@/api/client'
import {
  RealtimeStreamAdapter,
  type RealtimeConnectionState,
  type RealtimeTransportMode,
} from '@/api/realtime'
import { useAuthStore } from '@/stores/auth'
import type {
  ConfirmationState,
  ExecutionMetricsSnapshot,
  ExecutionRunItem,
  PublishedDatasetVersionItem,
  QualityReportResponse,
  ViewDataState,
} from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import StateView from '@/components/ui/StateView.vue'
import EChartBox from '@/components/charts/EChartBox.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

const activeModeTab = ref<'BATCH' | 'CDC'>('BATCH')
const runs = ref<ExecutionRunItem[]>([])
const selectedRun = ref<ExecutionRunItem | null>(null)
const qualityReport = ref<QualityReportResponse | null>(null)
const publishedDatasets = ref<PublishedDatasetVersionItem[]>([])
const metricsHistory = ref<ExecutionMetricsSnapshot[]>([])

// 实时流状态
const transportMode = ref<RealtimeTransportMode>('SSE')
const connectionState = ref<RealtimeConnectionState>('idle')
const confirmationState = ref<ConfirmationState>('REQUEST_SUBMITTED')
const actionNotice = ref<string | null>(null)
let adapter: RealtimeStreamAdapter | null = null

const filteredRuns = computed(() =>
  runs.value.filter((r) => r.execution_mode === activeModeTab.value),
)

function statusVariant(status: string) {
  if (status === 'SUCCEEDED' || status === 'STREAMING') return 'success'
  if (status === 'FAILED' || status === 'CHECKPOINT_LOST' || status === 'POSITION_EXPIRED')
    return 'danger'
  if (status === 'UNKNOWN' || status === 'RECONCILING' || status === 'DEGRADED') return 'warning'
  return 'info'
}

function confirmationLabel(cs: ConfirmationState): string {
  if (cs === 'ENGINE_CONFIRMED') return '执行引擎已确认 (ENGINE_CONFIRMED)'
  if (cs === 'RECONCILIATION_PENDING') return '状态待对账 (RECONCILIATION_PENDING)'
  return '请求已提交 (REQUEST_SUBMITTED)'
}

const throughputOption = computed<EChartsOption>(() => ({
  backgroundColor: 'transparent',
  tooltip: { trigger: 'axis' },
  legend: { data: ['吞吐量 (rows/s)', '整体拒绝率 (%)'], textStyle: { color: '#94a3b8' } },
  xAxis: {
    type: 'category',
    data: metricsHistory.value.map((m) => m.sampled_at.slice(11, 19)),
    axisLabel: { color: '#94a3b8' },
  },
  yAxis: [
    { type: 'value', name: 'RPS', axisLabel: { color: '#94a3b8' } },
    { type: 'value', name: 'Reject %', max: 100, axisLabel: { color: '#f87171' } },
  ],
  series: [
    {
      name: '吞吐量 (rows/s)',
      type: 'line',
      smooth: true,
      data: metricsHistory.value.map((m) => m.throughput_rps),
    },
    {
      name: '整体拒绝率 (%)',
      type: 'line',
      yAxisIndex: 1,
      data: metricsHistory.value.map((m) => Number((m.reject_rate_total * 100).toFixed(2))),
      markLine: {
        data: [{ yAxis: 20, name: '20% 硬阈值' }],
        lineStyle: { color: '#ef4444', type: 'dashed' },
      },
    },
  ],
}))

function stopRealtime(): void {
  adapter?.stop()
  adapter = null
}

async function loadExecutions(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const [execRes, pubRes] = await Promise.all([
      api.listExecutions(projectId),
      api.listPublishedDatasets(projectId),
    ])
    runs.value = execRes.items
    publishedDatasets.value = pubRes.items
    state.value = execRes.items.length === 0 ? 'empty' : 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'EXECUTIONS_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载执行列表失败',
        traceId: 'client-local',
      }
    }
  }
}

async function selectRun(run: ExecutionRunItem): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  stopRealtime()
  selectedRun.value = run
  confirmationState.value = run.confirmation_state
  metricsHistory.value = run.latest_metrics ? [run.latest_metrics] : []

  try {
    qualityReport.value = await api.getQualityReport(projectId, run.id)
  } catch {
    qualityReport.value = null
  }

  adapter = new RealtimeStreamAdapter({
    projectId,
    executionId: run.id,
    apiClient,
  })
  adapter.start({
    onTransportChange: (mode, conn) => {
      transportMode.value = mode
      connectionState.value = conn
    },
    onConfirmationStateChange: (cs) => {
      confirmationState.value = cs
    },
    onStateChanged: (ev) => {
      if (selectedRun.value && selectedRun.value.id === ev.execution_id) {
        selectedRun.value = {
          ...selectedRun.value,
          status: ev.to_status as ExecutionRunItem['status'],
          confirmation_state: ev.confirmation_state,
          external_job_id: ev.external_job_id ?? selectedRun.value.external_job_id,
        }
      }
    },
    onMetricsTick: (m) => {
      metricsHistory.value = [...metricsHistory.value.slice(-29), m]
    },
  })
}

async function handleCancel(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedRun.value) return
  await api.cancelExecution(projectId, selectedRun.value.id)
  actionNotice.value = '已通过 Tool Broker 提交受管取消/停止命令。'
}

async function handleReconcile(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedRun.value) return
  await api.reconcileExecution(projectId, selectedRun.value.id)
  actionNotice.value = '已触发 UNKNOWN 状态对账命令。'
}

async function handlePrepareRollback(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedRun.value) return
  const prep = await api.prepareRollback(projectId, {
    execution_id: selectedRun.value.id,
    rollback_type: selectedRun.value.execution_mode === 'CDC' ? 'CDC_RECOVERY' : 'SWAP_RESTORE',
  })
  actionNotice.value = `已创建受管回滚 Preparation (${prep.id})，请前往审批控制台完成审批与 Commit。`
}

onMounted(() => {
  void loadExecutions()
})

watch(
  () => auth.activeProject?.id,
  () => {
    stopRealtime()
    void loadExecutions()
  },
)

onBeforeUnmount(() => {
  stopRealtime()
})
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">运行中心与质量诊断</h1>
        <p class="text-xs text-muted-foreground">
          Batch 与 CDC 分模式状态机 · SSE 主通道 + HTTP 轮询兜底 · 显式确认状态区分
        </p>
      </div>
      <div class="flex gap-1 text-xs">
        <button
          type="button"
          class="rounded-md px-3 py-1.5"
          :class="activeModeTab === 'BATCH' ? 'bg-primary text-primary-foreground' : 'border'"
          @click="activeModeTab = 'BATCH'"
        >
          BATCH 批处理
        </button>
        <button
          type="button"
          class="rounded-md px-3 py-1.5"
          :class="activeModeTab === 'CDC' ? 'bg-primary text-primary-foreground' : 'border'"
          @click="activeModeTab = 'CDC'"
        >
          CDC 实时变更
        </button>
      </div>
    </div>

    <div
      v-if="actionNotice"
      class="rounded-md border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-xs text-sky-200"
    >
      {{ actionNotice }}
    </div>

    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      empty-title="暂无执行记录"
      empty-description="在审批控制台完成 Operator Commit 后，ExecutionRun 将在此实时追踪。"
      @retry="loadExecutions"
    >
      <div class="grid gap-4 lg:grid-cols-12">
        <!-- 左侧 ExecutionRun 列表 -->
        <Card title="ExecutionRun 列表" class="lg:col-span-4">
          <div class="space-y-2 text-xs">
            <div v-if="filteredRuns.length === 0" class="text-muted-foreground">
              当前模式 ({{ activeModeTab }}) 下暂无运行实例。
            </div>
            <button
              v-for="run in filteredRuns"
              :key="run.id"
              type="button"
              class="w-full rounded-md border p-3 text-left transition hover:bg-accent"
              :class="selectedRun?.id === run.id ? 'border-primary bg-primary/10' : ''"
              @click="selectRun(run)"
            >
              <div class="flex items-center justify-between">
                <span class="font-mono font-semibold">{{ (run.id || '').slice(0, 8) }}</span>
                <Badge :variant="statusVariant(run.status)">{{ run.status }}</Badge>
              </div>
              <div class="mt-1 font-mono text-[11px] text-muted-foreground">
                Zeta Job: {{ run.external_job_id || '待分配' }}
              </div>
            </button>
          </div>
        </Card>

        <!-- 右侧实时监控、质量报告与受管控制 -->
        <div class="space-y-4 lg:col-span-8">
          <Card
            v-if="selectedRun"
            :title="`执行详情 ${selectedRun.id}`"
            :subtitle="`实时通道: ${transportMode} (${connectionState}) · SeaTunnel Job ID: ${selectedRun.external_job_id || '-'}`"
          >
            <template #header>
              <Badge
                :variant="
                  confirmationState === 'ENGINE_CONFIRMED'
                    ? 'success'
                    : confirmationState === 'RECONCILIATION_PENDING'
                      ? 'warning'
                      : 'info'
                "
              >
                {{ confirmationLabel(confirmationState) }}
              </Badge>
            </template>

            <div class="space-y-4">
              <div class="flex flex-wrap gap-2 text-xs">
                <button
                  type="button"
                  class="rounded border border-rose-500/40 px-3 py-1 text-rose-300 hover:bg-rose-500/10"
                  @click="handleCancel"
                >
                  受管取消 / 停止
                </button>
                <button
                  type="button"
                  class="rounded border border-amber-500/40 px-3 py-1 text-amber-300 hover:bg-amber-500/10"
                  @click="handleReconcile"
                >
                  触发对账 (Reconcile)
                </button>
                <button
                  type="button"
                  class="rounded border px-3 py-1 hover:bg-accent"
                  @click="handlePrepareRollback"
                >
                  发起受管回滚 Prepare
                </button>
              </div>

              <EChartBox :option="throughputOption" height="240px" />

              <!-- 诊断建议（强制标记只读 / 无副作用） -->
              <div
                v-if="selectedRun.diagnosis"
                class="rounded-md border border-sky-500/30 bg-sky-500/5 p-3 text-xs"
              >
                <div class="flex items-center justify-between">
                  <span class="font-semibold">智能诊断分析</span>
                  <Badge variant="outline">只读建议 · 零副作用</Badge>
                </div>
                <p class="mt-1">{{ selectedRun.diagnosis.summary }}</p>
              </div>

              <!-- 质量报告与脱敏错误码分布 -->
              <div v-if="qualityReport" class="rounded-md border p-3 text-xs">
                <div class="flex items-center justify-between">
                  <span class="font-semibold">QualityContract 质量报告</span>
                  <Badge :variant="qualityReport.passed ? 'success' : 'danger'">
                    拒绝率: {{ (qualityReport.reject_rate * 100).toFixed(2) }}%
                  </Badge>
                </div>
                <div class="mt-2 space-y-1 font-mono text-[11px]">
                  <div v-for="err in qualityReport.redacted_error_distribution" :key="err.error_code">
                    {{ err.error_code }}: {{ err.count }} 行 (脱敏样本: {{ err.sample_redacted || '***' }})
                  </div>
                </div>
              </div>
            </div>
          </Card>

          <Card title="PublishedDatasetVersion 历史">
            <div v-if="publishedDatasets.length === 0" class="text-xs text-muted-foreground">
              暂无已发布目标表版本。
            </div>
            <div v-else class="space-y-1.5 text-xs font-mono">
              <div
                v-for="ds in publishedDatasets"
                :key="ds.id"
                class="flex items-center justify-between border-b py-1.5 last:border-0"
              >
                <span>{{ ds.target_table }} ({{ ds.row_count }} 行)</span>
                <Badge :variant="ds.is_active ? 'success' : 'outline'">
                  {{ ds.is_active ? 'ACTIVE' : 'HISTORICAL' }}
                </Badge>
              </div>
            </div>
          </Card>
        </div>
      </div>
    </StateView>
  </div>
</template>
