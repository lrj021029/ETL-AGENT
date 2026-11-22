<!-- 安全治理与 Benchmark：PDP 策略版本与 Diff、Evidence Ledger SHA256 链 + Ed25519 Checkpoint 校验、10K/100K/1M 数据集、L0/L1/L2 运行对比与 P0/P1 独立硬门槛 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { api } from '@/api'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type {
  BenchmarkDatasetItem,
  BenchmarkRunItem,
  EvidenceCheckpointItem,
  EvidenceEventItem,
  EvidenceVerifyResult,
  PolicyVersionItem,
  ViewDataState,
} from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import StateView from '@/components/ui/StateView.vue'
import EChartBox from '@/components/charts/EChartBox.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

const policies = ref<PolicyVersionItem[]>([])
const evidenceEvents = ref<EvidenceEventItem[]>([])
const checkpoints = ref<EvidenceCheckpointItem[]>([])
const verifyResult = ref<EvidenceVerifyResult | null>(null)
const datasets = ref<BenchmarkDatasetItem[]>([])
const runs = ref<BenchmarkRunItem[]>([])

const newDatasetScale = ref<'10K' | '100K' | '1M'>('10K')
const newDatasetSeed = ref(42)

const benchmarkChartOption = computed<EChartsOption>(() => ({
  backgroundColor: 'transparent',
  tooltip: { trigger: 'axis' },
  legend: {
    data: ['Plan Validity', 'SQL Safety', 'P0 Block Rate', 'Composite Score'],
    textStyle: { color: '#94a3b8' },
  },
  xAxis: {
    type: 'category',
    data: runs.value.map((r) => `${r.model_name} (${r.policy_version})`),
    axisLabel: { color: '#94a3b8' },
  },
  yAxis: { type: 'value', max: 100, axisLabel: { color: '#94a3b8' } },
  series: [
    {
      name: 'Plan Validity',
      type: 'bar',
      data: runs.value.map((r) => Number((r.raw_metrics.plan_validity * 100).toFixed(1))),
    },
    {
      name: 'SQL Safety',
      type: 'bar',
      data: runs.value.map((r) => Number((r.raw_metrics.sql_safety_detection * 100).toFixed(1))),
    },
    {
      name: 'P0 Block Rate',
      type: 'bar',
      data: runs.value.map((r) => Number((r.p0_block_rate * 100).toFixed(1))),
    },
    {
      name: 'Composite Score',
      type: 'line',
      data: runs.value.map((r) => Number((r.composite_score * 100).toFixed(1))),
    },
  ],
}))

async function loadGovernance(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const [polRes, evRes, cpRes, dsRes, runRes] = await Promise.all([
      api.listPolicies(projectId),
      api.listEvidenceEvents(projectId),
      api.listEvidenceCheckpoints(projectId),
      api.listBenchmarkDatasets(projectId),
      api.listBenchmarkRuns(projectId),
    ])
    policies.value = polRes.items
    evidenceEvents.value = evRes.items
    checkpoints.value = cpRes.items
    datasets.value = dsRes.items
    runs.value = runRes.items
    state.value = 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'GOVERNANCE_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载安全治理与 Benchmark 数据失败',
        traceId: 'client-local',
      }
    }
  }
}

async function handleVerifyChain(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  verifyResult.value = await api.verifyEvidenceChain(projectId)
}

async function handleGenerateDataset(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  await api.generateBenchmarkDataset(projectId, {
    name: `bench-${newDatasetScale.value.toLowerCase()}-seed${newDatasetSeed.value}`,
    scale: newDatasetScale.value,
    seed: newDatasetSeed.value,
  })
  await loadGovernance()
}

async function handleTriggerRun(level: 'L0' | 'L1' | 'L2'): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || datasets.value.length === 0) return
  await api.triggerBenchmarkRun(projectId, {
    dataset_id: datasets.value[0].id,
    level,
    policy_version: policies.value[0]?.version || 'v1.0',
  })
  await loadGovernance()
}

onMounted(() => {
  void loadGovernance()
})

watch(
  () => auth.activeProject?.id,
  () => {
    void loadGovernance()
  },
)
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">安全治理与 Benchmark</h1>
        <p class="text-xs text-muted-foreground">
          PDP 策略版本回归 · Evidence Ledger SHA256 + Ed25519 Checkpoint 校验 · L0/L1/L2 基准评测
        </p>
      </div>
      <Badge variant="danger" data-testid="p0-p1-hard-gate-badge">
        P0/P1 Independent Hard Gate 独立硬门槛（不可被综合评分抵消）
      </Badge>
    </div>

    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      @retry="loadGovernance"
    >
      <div class="space-y-5">
        <!-- Evidence Ledger & Ed25519 Checkpoint 校验 -->
        <Card title="Evidence Ledger 链与 Ed25519 Checkpoint 校验" subtitle="只读防篡改审计账本">
          <template #header>
            <button
              type="button"
              class="rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground"
              @click="handleVerifyChain"
            >
              校验 SHA256 链与 Ed25519 签名
            </button>
          </template>

          <div
            v-if="verifyResult"
            class="mb-3 rounded border p-3 text-xs"
            :class="
              verifyResult.chain_valid && verifyResult.checkpoint_signatures_valid
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
                : 'border-rose-500/40 bg-rose-500/10 text-rose-200'
            "
          >
            <div class="font-semibold">{{ verifyResult.message }}</div>
            <div class="mt-1 font-mono text-[11px]">
              已校验事件数: {{ verifyResult.verified_events_count }} | Hash Chain:
              {{ verifyResult.chain_valid }} | Ed25519 Checkpoints:
              {{ verifyResult.checkpoint_signatures_valid }}
            </div>
          </div>

          <div class="grid gap-4 md:grid-cols-2 text-xs">
            <div>
              <h4 class="mb-1.5 font-semibold">最近 Evidence 事件 ({{ evidenceEvents.length }})</h4>
              <div class="max-h-40 space-y-1 overflow-y-auto font-mono text-[11px]">
                <div v-for="ev in evidenceEvents" :key="ev.id" class="border-b py-1">
                  #{{ ev.seq }} [{{ ev.event_type }}] {{ ev.sha256_hash.slice(0, 14) }}…
                </div>
              </div>
            </div>
            <div>
              <h4 class="mb-1.5 font-semibold">Ed25519 签名 Checkpoints ({{ checkpoints.length }})</h4>
              <div class="max-h-40 space-y-1 overflow-y-auto font-mono text-[11px]">
                <div v-for="cp in checkpoints" :key="cp.id" class="border-b py-1">
                  seq {{ cp.from_seq }}..{{ cp.to_seq }} · key={{ cp.key_id }} · sig={{
                    cp.ed25519_signature.slice(0, 16)
                  }}…
                </div>
              </div>
            </div>
          </div>
        </Card>

        <!-- Benchmark 数据集与 L0/L1/L2 运行 -->
        <Card title="Benchmark 数据集 (10K / 100K / 1M) 与 L0/L1/L2 评测">
          <template #header>
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <select v-model="newDatasetScale" class="rounded border bg-background px-2 py-1">
                <option value="10K">10K</option>
                <option value="100K">100K</option>
                <option value="1M">1M</option>
              </select>
              <input
                v-model.number="newDatasetSeed"
                type="number"
                class="w-20 rounded border bg-background px-2 py-1 font-mono"
              />
              <button
                type="button"
                class="rounded border px-2.5 py-1 hover:bg-accent"
                @click="handleGenerateDataset"
              >
                生成确定性数据集
              </button>
              <button
                type="button"
                class="rounded bg-primary px-2.5 py-1 text-primary-foreground"
                @click="handleTriggerRun('L0')"
              >
                触发 L0
              </button>
              <button
                type="button"
                class="rounded bg-primary px-2.5 py-1 text-primary-foreground"
                @click="handleTriggerRun('L1')"
              >
                触发 L1
              </button>
              <button
                type="button"
                class="rounded bg-primary px-2.5 py-1 text-primary-foreground"
                @click="handleTriggerRun('L2')"
              >
                触发 L2
              </button>
            </div>
          </template>

          <EChartBox :option="benchmarkChartOption" height="260px" />

          <div class="mt-4 overflow-x-auto text-xs">
            <table class="w-full text-left">
              <thead class="border-b text-muted-foreground">
                <tr>
                  <th class="py-1.5 pr-3">Level</th>
                  <th class="py-1.5 pr-3">模型 / 策略版本</th>
                  <th class="py-1.5 pr-3">P0/P1 硬门槛</th>
                  <th class="py-1.5 pr-3">综合评分 (权重版本)</th>
                  <th class="py-1.5">总判定</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <tr v-for="r in runs" :key="r.id">
                  <td class="py-1.5 pr-3 font-mono">{{ r.level }}</td>
                  <td class="py-1.5 pr-3">{{ r.model_name }} / {{ r.policy_version }}</td>
                  <td class="py-1.5 pr-3">
                    <Badge :variant="r.p0_p1_hard_gate_passed ? 'success' : 'danger'">
                      {{ r.p0_p1_hard_gate_passed ? 'HARD GATE PASS' : 'HARD GATE FAILED' }}
                    </Badge>
                  </td>
                  <td class="py-1.5 pr-3 font-mono">
                    {{ (r.composite_score * 100).toFixed(1) }} ({{ r.scoring_profile_version }})
                  </td>
                  <td class="py-1.5">
                    <Badge :variant="r.overall_verdict === 'PASSED' ? 'success' : 'danger'">
                      {{ r.overall_verdict }}
                    </Badge>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </Card>

        <!-- PDP 策略版本与 Diff -->
        <Card title="PDP 策略版本与 Diff 审查">
          <div v-if="policies.length === 0" class="text-xs text-muted-foreground">
            暂无策略版本记录。
          </div>
          <div v-else class="space-y-2 text-xs">
            <div v-for="p in policies" :key="p.id" class="rounded border p-3">
              <div class="flex items-center justify-between font-mono">
                <span class="font-semibold">{{ p.version }}</span>
                <span class="text-muted-foreground">{{ p.created_by }} · {{ p.created_at }}</span>
              </div>
              <pre
                v-if="p.diff_from_prev"
                class="mt-2 overflow-x-auto rounded bg-zinc-950 p-2 font-mono text-[11px] text-zinc-300"
              >{{ p.diff_from_prev }}</pre>
            </div>
          </div>
        </Card>
      </div>
    </StateView>
  </div>
</template>
