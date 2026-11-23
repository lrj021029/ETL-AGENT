<!-- Pipeline Studio：PRD §4.3 三区布局（需求澄清区 / 拓扑与制品审查区 / 确定性门禁与冻结区） -->
<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type {
  AgentThreadState,
  ConnectionItem,
  GateValidationReport,
  PipelineDraftArtifacts,
  PipelineItem,
  PipelineVersionItem,
  ViewDataState,
} from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import Modal from '@/components/ui/Modal.vue'
import StateView from '@/components/ui/StateView.vue'
import MonacoCodeEditor from '@/components/editor/MonacoCodeEditor.vue'
import PipelineDagFlow from '@/components/studio/PipelineDagFlow.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

const connections = ref<ConnectionItem[]>([])
const pipelines = ref<PipelineItem[]>([])
const selectedPipelineId = ref<string>('')
const newPipelineName = ref('')
const creatingPipeline = ref(false)
const sourceConnId = ref<string>('')
const sinkConnId = ref<string>('')
const executionMode = ref<'BATCH' | 'CDC'>('BATCH')
const nlRequirement = ref('')

// LangGraph Thread 状态
const threadState = ref<AgentThreadState | null>(null)
const clarificationAnswers = ref<Record<string, string>>({})
const agentBusy = ref(false)

// 中心审查区 Tab
type StudioTab = 'DAG' | 'DSL' | 'SQL_HOCON' | 'SCHEMA_MASK' | 'CONTRACTS'
const activeTab = ref<StudioTab>('DAG')
const showDiff = ref(false)
const originalSql = ref('')
const originalHocon = ref('')

const draft = ref<PipelineDraftArtifacts>({
  etl_plan_dsl: {},
  dag_nodes: [],
  dag_edges: [],
  sql_transformation: '-- 等待 LangGraph 生成或手动编辑受限 SELECT SQL',
  seatunnel_hocon: '# 等待生成 SeaTunnel HOCON',
  schema_mapping_diff: [],
  quality_contract: { rules: [], restricted_sql_enabled: false },
  runtime_supervision_contract: {
    max_read_rows: 1000000,
    max_write_bytes: 1073741824,
    max_duration_seconds: 3600,
    max_amplification_ratio: 3.0,
    reject_rate_threshold_pct: 20,
  },
})

// 右/下区：确定性门禁与冻结版本
const gateReport = ref<GateValidationReport | null>(null)
const frozenVersion = ref<PipelineVersionItem | null>(null)
const actionMessage = ref<string | null>(null)

// Schedule 配置弹窗
const scheduleModalOpen = ref(false)
const scheduleForm = ref({
  standing_preparation_id: '',
  cron_expression: '0 2 * * *',
  misfire_policy: 'FORBID' as 'FORBID' | 'FIRE_ONCE',
  concurrency_policy: 'SKIP' as 'SKIP' | 'FORBID',
})

async function loadStudioContext(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const [connRes, pipeRes] = await Promise.all([
      api.listConnections(projectId),
      api.listPipelines(projectId),
    ])
    connections.value = connRes.items
    pipelines.value = pipeRes.items
    if (connRes.items.length > 0) {
      sourceConnId.value = sourceConnId.value || connRes.items[0].id
      sinkConnId.value = sinkConnId.value || connRes.items[Math.min(1, connRes.items.length - 1)].id
    }
    if (pipeRes.items.length > 0 && !selectedPipelineId.value) {
      selectedPipelineId.value = pipeRes.items[0].id
    }
    state.value = 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'STUDIO_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载 Studio 上下文失败',
        traceId: 'client-local',
      }
    }
  }
}

function applyDraftFromThread(ts: AgentThreadState): void {
  if (ts.draft_artifacts) {
    draft.value = ts.draft_artifacts
    originalSql.value = ts.draft_artifacts.sql_transformation
    originalHocon.value = ts.draft_artifacts.seatunnel_hocon
  }
}

async function handleInvokeAgent(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !sourceConnId.value || !sinkConnId.value) return
  agentBusy.value = true
  actionMessage.value = null
  try {
    let tid = threadState.value?.thread_id
    if (!tid) {
      const created = await api.createAgentThread(projectId, {
        source_connection_id: sourceConnId.value,
        sink_connection_id: sinkConnId.value,
      })
      tid = created.thread_id
    }
    const res = await api.invokeAgentThread(projectId, tid, {
      requirement: nlRequirement.value,
      execution_mode: executionMode.value,
    })
    threadState.value = res
    applyDraftFromThread(res)
  } catch (err) {
    actionMessage.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  } finally {
    agentBusy.value = false
  }
}

async function handleResumeAgent(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !threadState.value) return
  agentBusy.value = true
  try {
    const res = await api.resumeAgentThread(projectId, threadState.value.thread_id, {
      answers: clarificationAnswers.value,
    })
    threadState.value = res
    applyDraftFromThread(res)
  } finally {
    agentBusy.value = false
  }
}

async function handleCreatePipeline(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !sourceConnId.value || !sinkConnId.value) return
  const name = newPipelineName.value.trim() || `orders-sync-${Date.now().toString().slice(-4)}`
  creatingPipeline.value = true
  actionMessage.value = null
  try {
    const created = await api.createPipeline(projectId, {
      name,
      description: '由 Pipeline Studio 创建的受管同步流水线',
      source_connection_id: sourceConnId.value,
      sink_connection_id: sinkConnId.value,
      execution_mode: executionMode.value,
    })
    pipelines.value = [created, ...pipelines.value]
    selectedPipelineId.value = created.id
    newPipelineName.value = ''
    actionMessage.value = `已创建 Pipeline「${created.name}」，可继续执行 LangGraph 生成与门禁冻结。`
  } catch (err) {
    actionMessage.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  } finally {
    creatingPipeline.value = false
  }
}

async function handleRunDeterministicGate(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedPipelineId.value) {
    actionMessage.value = '请先选择或新建一个 Pipeline 定义后再执行确定性门禁校验。'
    return
  }
  actionMessage.value = null
  try {
    const report = await api.validateDraft(projectId, selectedPipelineId.value, draft.value)
    gateReport.value = report
    actionMessage.value = report.passed
      ? '确定性门禁 5 项校验全部通过，可执行版本冻结！'
      : '确定性门禁未通过，请根据下方失败原因修复后再校验。'
  } catch (err) {
    actionMessage.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleFreezeVersion(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedPipelineId.value) return
  actionMessage.value = null
  try {
    const frozen = await api.freezePipelineVersion(projectId, selectedPipelineId.value, {
      draft: draft.value,
      comment: 'Studio 确定性门禁通过后冻结版本',
    })
    frozenVersion.value = frozen
    actionMessage.value = `已冻结不可变版本 v${frozen.version_number} (SHA256: ${(frozen.sha256_digest || '').slice(0, 16)}…)`
  } catch (err) {
    actionMessage.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleCreatePreparation(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !frozenVersion.value) return
  actionMessage.value = null
  try {
    const prep = await api.createPreparation(projectId, {
      pipeline_version_id: frozenVersion.value.id,
      preparation_type: 'EXECUTION',
    })
    actionMessage.value = `已发起 Harness Preparation (${prep.id.slice(0, 8)} · 风险等级 ${prep.risk_level} · 状态 ${prep.status})，请前往「审批控制台与 Preparation」完成双人审批与 Operator Commit！`
  } catch (err) {
    actionMessage.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleCreateSchedule(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedPipelineId.value || !frozenVersion.value) return
  await api.createSchedule(projectId, {
    pipeline_id: selectedPipelineId.value,
    pipeline_version_id: frozenVersion.value.id,
    standing_preparation_id: scheduleForm.value.standing_preparation_id,
    cron_expression: scheduleForm.value.cron_expression,
    misfire_policy: scheduleForm.value.misfire_policy,
    concurrency_policy: scheduleForm.value.concurrency_policy,
  })
  scheduleModalOpen.value = false
  actionMessage.value = 'Schedule 已创建（绑定受管 StandingSchedulePreparation）。'
}

onMounted(() => {
  void loadStudioContext()
})

watch(
  () => auth.activeProject?.id,
  () => {
    void loadStudioContext()
  },
)
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">Pipeline Studio</h1>
        <p class="text-xs text-muted-foreground">
          LangGraph 只读澄清与候选生成 → 确定性门禁校验 → 不可变 PipelineVersion + SHA256 冻结
        </p>
      </div>
      <div class="flex items-center gap-2">
        <Badge v-if="threadState" :variant="threadState.llm_redaction_status.redacted ? 'success' : 'warning'">
          外部 LLM 脱敏: {{ threadState.llm_redaction_status.redacted ? '已脱敏' : '未脱敏' }}
          ({{ threadState.llm_redaction_status.policy_version }})
        </Badge>
        <Badge variant="outline">
          LangGraph 状态: {{ threadState?.status || 'IDLE' }}
        </Badge>
      </div>
    </div>

    <div
      v-if="actionMessage"
      class="rounded-md border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-xs text-sky-200"
    >
      {{ actionMessage }}
    </div>

    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      @retry="loadStudioContext"
    >
      <div class="grid gap-4 xl:grid-cols-12">
        <!-- 区 1：需求输入与澄清区 -->
        <Card title="1. 需求输入与 LangGraph 澄清" class="xl:col-span-3">
          <div class="space-y-3 text-xs">
            <div>
              <label class="mb-1 block font-medium">目标 Pipeline</label>
              <select v-model="selectedPipelineId" class="w-full rounded border bg-background px-2 py-1.5">
                <option value="">-- 选择 Pipeline --</option>
                <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
              <div class="mt-1.5 flex gap-1.5">
                <input
                  v-model="newPipelineName"
                  placeholder="新 Pipeline 名称（如 mysql-to-doris-orders）"
                  class="flex-1 rounded border bg-background px-2 py-1 text-xs"
                />
                <button
                  type="button"
                  :disabled="creatingPipeline || !sourceConnId || !sinkConnId"
                  class="rounded border border-primary/40 bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/20 disabled:opacity-40"
                  @click="handleCreatePipeline"
                >
                  {{ creatingPipeline ? '创建中…' : '+ 新建' }}
                </button>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="mb-1 block font-medium">Source 连接</label>
                <select v-model="sourceConnId" class="w-full rounded border bg-background px-2 py-1.5">
                  <option v-for="c in connections" :key="c.id" :value="c.id">{{ c.name }}</option>
                </select>
              </div>
              <div>
                <label class="mb-1 block font-medium">Sink 连接</label>
                <select v-model="sinkConnId" class="w-full rounded border bg-background px-2 py-1.5">
                  <option v-for="c in connections" :key="c.id" :value="c.id">{{ c.name }}</option>
                </select>
              </div>
            </div>

            <div>
              <label class="mb-1 block font-medium">同步模式</label>
              <select v-model="executionMode" class="w-full rounded border bg-background px-2 py-1.5">
                <option value="BATCH">BATCH（批处理）</option>
                <option value="CDC">CDC（实时变更捕获）</option>
              </select>
            </div>

            <div>
              <label class="mb-1 block font-medium">自然语言集成需求</label>
              <textarea
                v-model="nlRequirement"
                rows="4"
                placeholder="例如：将 MySQL demo_db.orders 增量同步至 Doris ods_orders，手机号脱敏且错误率超 20% 中断"
                class="w-full rounded border bg-background p-2 text-xs"
              />
            </div>

            <button
              type="button"
              :disabled="agentBusy || !nlRequirement.trim()"
              class="w-full rounded-md bg-primary px-3 py-2 font-medium text-primary-foreground disabled:opacity-50"
              @click="handleInvokeAgent"
            >
              {{ agentBusy ? 'LangGraph 运行中…' : '生成候选 EtlPlan / SQL / HOCON' }}
            </button>

            <!-- LangGraph Clarification Interrupt 恢复表单 -->
            <div
              v-if="threadState?.status === 'INTERRUPTED' && threadState.interrupt_questions?.length"
              class="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3"
            >
              <div class="font-semibold text-amber-300">需要补充澄清参数 (PG Checkpoint)</div>
              <div v-for="q in threadState.interrupt_questions" :key="q.field" class="space-y-1">
                <label class="block text-[11px]">{{ q.prompt }}</label>
                <input
                  v-model="clarificationAnswers[q.field]"
                  type="text"
                  class="w-full rounded border bg-background px-2 py-1 text-xs"
                />
              </div>
              <button
                type="button"
                class="w-full rounded bg-amber-500 px-3 py-1.5 font-medium text-slate-950"
                @click="handleResumeAgent"
              >
                提交澄清并恢复 Graph
              </button>
            </div>
          </div>
        </Card>

        <!-- 区 2：方案配置与拓扑审查区 -->
        <Card title="2. 方案配置与拓扑审查" class="xl:col-span-6">
          <template #header>
            <div class="flex flex-wrap gap-1 text-xs">
              <button
                v-for="tab in [
                  { id: 'DAG', label: 'DAG 拓扑' },
                  { id: 'DSL', label: 'EtlPlan DSL' },
                  { id: 'SQL_HOCON', label: 'SQL & HOCON' },
                  { id: 'SCHEMA_MASK', label: '映射 & 脱敏 Diff' },
                  { id: 'CONTRACTS', label: '质量与监督契约' },
                ]"
                :key="tab.id"
                type="button"
                class="rounded px-2.5 py-1"
                :class="activeTab === tab.id ? 'bg-primary text-primary-foreground' : 'border hover:bg-accent'"
                @click="activeTab = tab.id as StudioTab"
              >
                {{ tab.label }}
              </button>
            </div>
          </template>

          <PipelineDagFlow
            v-if="activeTab === 'DAG'"
            :nodes="draft.dag_nodes"
            :edges="draft.dag_edges"
          />

          <MonacoCodeEditor
            v-else-if="activeTab === 'DSL'"
            :model-value="JSON.stringify(draft.etl_plan_dsl, null, 2)"
            language="json"
            read-only
          />

          <div v-else-if="activeTab === 'SQL_HOCON'" class="space-y-3">
            <div class="flex items-center justify-between text-xs">
              <span class="text-muted-foreground">
                执行位置理由：{{ draft.sql_execution_location_reason || '仅允许受限 SELECT 白名单' }}
              </span>
              <label class="flex items-center gap-1.5">
                <input v-model="showDiff" type="checkbox" />
                <span>开启 Diff 对比</span>
              </label>
            </div>
            <div>
              <div class="mb-1 text-xs font-medium">SQL Transformation（修改后须重跑确定性门禁）</div>
              <MonacoCodeEditor
                v-model="draft.sql_transformation"
                :original-value="originalSql"
                :diff-mode="showDiff"
                language="sql"
                height="180px"
              />
            </div>
            <div>
              <div class="mb-1 text-xs font-medium">SeaTunnel HOCON</div>
              <MonacoCodeEditor
                v-model="draft.seatunnel_hocon"
                :original-value="originalHocon"
                :diff-mode="showDiff"
                language="ini"
                height="180px"
              />
            </div>
          </div>

          <div v-else-if="activeTab === 'SCHEMA_MASK'" class="overflow-x-auto text-xs">
            <table class="w-full text-left">
              <thead class="border-b text-muted-foreground">
                <tr>
                  <th class="py-1.5 pr-3">源字段</th>
                  <th class="py-1.5 pr-3">目标字段</th>
                  <th class="py-1.5 pr-3">敏感分级</th>
                  <th class="py-1.5">脱敏后规则</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <tr v-if="draft.schema_mapping_diff.length === 0">
                  <td colspan="4" class="py-4 text-center text-muted-foreground">暂无字段映射差异数据</td>
                </tr>
                <tr v-for="m in draft.schema_mapping_diff" :key="m.source_column">
                  <td class="py-1.5 pr-3 font-mono">{{ m.source_column }} ({{ m.source_type }})</td>
                  <td class="py-1.5 pr-3 font-mono">{{ m.target_column }} ({{ m.target_type }})</td>
                  <td class="py-1.5 pr-3">{{ m.sensitivity_before || 'NONE' }}</td>
                  <td class="py-1.5 font-mono text-emerald-400">{{ m.masking_after || 'PASSTHROUGH' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-else class="space-y-3 text-xs">
            <div class="rounded border p-3">
              <div class="font-semibold">RuntimeSupervisionContract</div>
              <div class="mt-2 grid grid-cols-2 gap-2 font-mono">
                <div>max_read_rows: {{ draft.runtime_supervision_contract.max_read_rows }}</div>
                <div>max_write_bytes: {{ draft.runtime_supervision_contract.max_write_bytes }}</div>
                <div>max_duration_seconds: {{ draft.runtime_supervision_contract.max_duration_seconds }}</div>
                <div>reject_rate_threshold_pct: {{ draft.runtime_supervision_contract.reject_rate_threshold_pct }}%</div>
              </div>
            </div>
            <div class="rounded border p-3">
              <div class="font-semibold">QualityContract 规则 ({{ draft.quality_contract.rules.length }})</div>
              <ul class="mt-2 space-y-1 font-mono">
                <li v-for="r in draft.quality_contract.rules" :key="r.rule_id">
                  [{{ r.rule_type }}] {{ r.column || '*' }}: {{ r.expression }}
                </li>
              </ul>
            </div>
          </div>
        </Card>

        <!-- 区 3：确定性门禁与提交冻结区 -->
        <Card title="3. 确定性门禁与版本冻结" class="xl:col-span-3">
          <div class="space-y-3 text-xs">
            <button
              type="button"
              class="w-full rounded-md border border-primary/50 bg-primary/15 px-3 py-2 font-medium text-primary hover:bg-primary/25"
              @click="handleRunDeterministicGate"
            >
              执行确定性门禁校验
            </button>

            <div v-if="gateReport" class="space-y-2 rounded border p-2.5">
              <div class="flex items-center justify-between">
                <Badge :variant="gateReport.passed ? 'success' : 'danger'">
                  {{ gateReport.passed ? 'GATE PASSED' : 'GATE FAILED' }}
                </Badge>
                <span class="font-mono text-[11px] text-muted-foreground">
                  修复轮次: {{ gateReport.repair_loop_count }} / {{ gateReport.max_repair_loops }}
                </span>
              </div>
              <div v-for="chk in gateReport.checks" :key="chk.gate_name" class="border-t pt-1.5">
                <div class="flex justify-between font-medium">
                  <span>{{ chk.gate_name }}</span>
                  <span :class="chk.passed ? 'text-emerald-400' : 'text-rose-400'">
                    {{ chk.passed ? 'PASS' : 'FAIL' }}
                  </span>
                </div>
                <p v-for="e in chk.errors" :key="e" class="text-[11px] text-rose-300">{{ e }}</p>
                <p v-for="w in chk.warnings" :key="w" class="text-[11px] text-amber-300">{{ w }}</p>
              </div>
            </div>

            <button
              type="button"
              :disabled="!gateReport?.passed"
              class="w-full rounded-md bg-emerald-600 px-3 py-2 font-medium text-white hover:bg-emerald-500 disabled:opacity-40"
              @click="handleFreezeVersion"
            >
              冻结不可变 PipelineVersion
            </button>

            <div v-if="frozenVersion" class="space-y-1.5 rounded border bg-muted/40 p-2.5 font-mono text-[11px]">
              <div class="font-semibold text-emerald-400">
                v{{ frozenVersion.version_number }} 已冻结
              </div>
              <div class="break-all">SHA256: {{ frozenVersion.sha256_digest }}</div>
              <button
                type="button"
                class="mt-2 w-full rounded bg-primary px-2 py-1.5 font-sans text-xs font-medium text-primary-foreground hover:opacity-90"
                @click="handleCreatePreparation"
              >
                发起 Harness Prepare 审批单
              </button>
              <button
                type="button"
                class="mt-1.5 w-full rounded border px-2 py-1 font-sans text-xs hover:bg-accent"
                @click="scheduleModalOpen = true"
              >
                配置受管 Schedule
              </button>
            </div>
          </div>
        </Card>
      </div>
    </StateView>

    <!-- 受管 Schedule 配置弹窗 -->
    <Modal
      :open="scheduleModalOpen"
      title="配置受管 Schedule"
      description="注意：Schedule 必须绑定已审批通过的 StandingSchedulePreparation，禁止绕过 Prepare / Approve / Commit。"
      @close="scheduleModalOpen = false"
    >
      <form class="space-y-3 text-xs" @submit.prevent="handleCreateSchedule">
        <div>
          <label class="mb-1 block font-medium">StandingSchedulePreparation ID（必填）</label>
          <input
            v-model="scheduleForm.standing_preparation_id"
            required
            placeholder="输入已批准的 StandingSchedulePreparation UUID"
            class="w-full rounded border bg-background px-2.5 py-1.5 font-mono"
          />
        </div>
        <div>
          <label class="mb-1 block font-medium">Cron 表达式</label>
          <input
            v-model="scheduleForm.cron_expression"
            required
            class="w-full rounded border bg-background px-2.5 py-1.5 font-mono"
          />
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="mb-1 block font-medium">Misfire 策略</label>
            <select v-model="scheduleForm.misfire_policy" class="w-full rounded border bg-background px-2 py-1.5">
              <option value="FORBID">FORBID（推荐）</option>
              <option value="FIRE_ONCE">FIRE_ONCE</option>
            </select>
          </div>
          <div>
            <label class="mb-1 block font-medium">并发策略</label>
            <select v-model="scheduleForm.concurrency_policy" class="w-full rounded border bg-background px-2 py-1.5">
              <option value="SKIP">SKIP（推荐）</option>
              <option value="FORBID">FORBID</option>
            </select>
          </div>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button
            type="button"
            class="rounded border px-3 py-1.5"
            @click="scheduleModalOpen = false"
          >
            取消
          </button>
          <button
            type="submit"
            class="rounded bg-primary px-3 py-1.5 font-medium text-primary-foreground"
          >
            绑定并创建 Schedule
          </button>
        </div>
      </form>
    </Modal>
  </div>
</template>
