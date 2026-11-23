<!-- 审批控制台与 Preparation：展示冻结事实、双审批槽位、Operator Commit 与后端 SoD/P0 原文拒绝理由 -->
<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ApiClientError, generateIdempotencyKey } from '@/api/client'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { CommitResponse, PreparationItem, RiskLevel, ViewDataState } from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import Drawer from '@/components/ui/Drawer.vue'
import Modal from '@/components/ui/Modal.vue'
import StateView from '@/components/ui/StateView.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

const preparations = ref<PreparationItem[]>([])
const selectedPrep = ref<PreparationItem | null>(null)
const drawerOpen = ref(false)
const approvalReason = ref('')
const commitModalOpen = ref(false)
const commitIdempotencyKey = ref('')
const commitResult = ref<CommitResponse | null>(null)

// 显式展示后端返回的 SoD / P0 / 状态机拒绝原因原文（不依赖隐藏按钮）
const actionError = ref<{ code: string; message: string; traceId: string } | null>(null)

function riskVariant(level: RiskLevel) {
  if (level === 'P0') return 'danger'
  if (level === 'P1') return 'warning'
  if (level === 'P2') return 'info'
  return 'success'
}

async function loadPreparations(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const res = await api.listPreparations(projectId)
    preparations.value = res.items
    state.value = res.items.length === 0 ? 'empty' : 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'PREPARATIONS_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载 Preparation 列表失败',
        traceId: 'client-local',
      }
    }
  }
}

function openDetail(prep: PreparationItem): void {
  selectedPrep.value = prep
  actionError.value = null
  commitResult.value = null
  drawerOpen.value = true
}

async function handleSubmitApproval(
  slotType: 'DATA_CHECKER' | 'SECURITY_CHECKER',
  decision: 'APPROVED' | 'REJECTED',
): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedPrep.value) return
  actionError.value = null
  try {
    await api.submitApproval(projectId, selectedPrep.value.id, {
      slot_type: slotType,
      decision,
      reason: approvalReason.value.trim() || `${slotType} ${decision}`,
    })
    const refreshed = await api.getPreparation(projectId, selectedPrep.value.id)
    selectedPrep.value = refreshed
    await loadPreparations()
  } catch (err) {
    if (err instanceof ApiClientError) {
      actionError.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      actionError.value = {
        code: 'APPROVAL_ERROR',
        message: err instanceof Error ? err.message : '审批提交失败',
        traceId: 'client-local',
      }
    }
  }
}

function openCommitModal(): void {
  commitIdempotencyKey.value = generateIdempotencyKey('commit')
  actionError.value = null
  commitModalOpen.value = true
}

async function handleConfirmCommit(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedPrep.value) return
  actionError.value = null
  try {
    const res = await api.commitPreparation(
      projectId,
      selectedPrep.value.id,
      commitIdempotencyKey.value,
    )
    commitResult.value = res
    commitModalOpen.value = false
    const refreshed = await api.getPreparation(projectId, selectedPrep.value.id)
    selectedPrep.value = refreshed
    await loadPreparations()
  } catch (err) {
    commitModalOpen.value = false
    if (err instanceof ApiClientError) {
      actionError.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      actionError.value = {
        code: 'COMMIT_ERROR',
        message: err instanceof Error ? err.message : 'Commit 执行失败',
        traceId: 'client-local',
      }
    }
  }
}

onMounted(() => {
  void loadPreparations()
})

watch(
  () => auth.activeProject?.id,
  () => {
    void loadPreparations()
  },
)
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">审批控制台与 Preparation</h1>
        <p class="text-xs text-muted-foreground">
          Prepare 冻结事实 → Data/Security Checker 独立双签（严格 SoD）→ Operator Commit 签发单次 Ed25519 Capability
        </p>
      </div>
      <button
        type="button"
        class="rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
        @click="loadPreparations"
      >
        刷新审批队列
      </button>
    </div>

    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      empty-title="暂无 Preparation 单据"
      empty-description="在 Pipeline Studio 冻结版本并发起 Prepare 后，审批单将显示在此处。"
      @retry="loadPreparations"
    >
      <div class="space-y-3">
        <Card
          v-for="prep in preparations"
          :key="prep.id"
          :title="`Preparation ${(prep.id || '').slice(0, 8)} · ${prep.preparation_type}`"
          :subtitle="`环境: ${(prep.environment || 'prod').toUpperCase()} · 指纹: ${(prep.input_fingerprint || '').slice(0, 16)}…`"
        >
          <template #header>
            <div class="flex items-center gap-2">
              <Badge :variant="riskVariant(prep.risk_level)">{{ prep.risk_level }}</Badge>
              <Badge :variant="prep.status === 'INVALIDATED' ? 'danger' : 'outline'">
                {{ prep.status }}
              </Badge>
              <button
                type="button"
                class="rounded border px-2.5 py-1 text-xs hover:bg-accent"
                @click="openDetail(prep)"
              >
                审查冻结事实与审批
              </button>
            </div>
          </template>

          <div class="flex flex-wrap gap-3 text-xs text-muted-foreground">
            <span>数据分级: {{ prep.data_classification || 'CONFIDENTIAL' }}</span>
            <span>资源范围: {{ (prep.resource_scope || []).join(', ') || '-' }}</span>
            <span>发起人: {{ prep.created_by }}</span>
          </div>
        </Card>
      </div>
    </StateView>

    <!-- Preparation 冻结事实与审批抽屉 -->
    <Drawer
      :open="drawerOpen"
      :title="selectedPrep ? `Preparation 详情 (${selectedPrep.risk_level})` : 'Preparation 详情'"
      subtitle="Checker 仅审查冻结事实，不可直接修改 Prepare 内容"
      @close="drawerOpen = false"
    >
      <div v-if="selectedPrep" class="space-y-4 text-xs">
        <!-- 后端 SoD / P0 / 失效原因原文展示 -->
        <div
          v-if="actionError"
          role="alert"
          data-testid="backend-rejection-alert"
          class="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-rose-200"
        >
          <div class="font-semibold text-rose-300">
            {{ actionError.code }}: {{ actionError.message }}
          </div>
          <div class="mt-1 font-mono text-[11px] opacity-80">Trace ID: {{ actionError.traceId }}</div>
        </div>

        <div
          v-if="selectedPrep.status === 'INVALIDATED' && selectedPrep.invalidation_reason"
          class="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-amber-200"
        >
          <div class="font-semibold">Preparation 已失效</div>
          <p class="mt-1">{{ selectedPrep.invalidation_reason }}</p>
        </div>

        <div
          v-if="commitResult"
          class="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-emerald-200"
        >
          <div class="font-semibold">Operator Commit 已受理</div>
          <div class="mt-1 font-mono text-[11px]">
            Execution ID: {{ commitResult.execution_id }} | Command: {{ commitResult.command_id }} |
            确认状态: {{ commitResult.confirmation_state }}
          </div>
        </div>

        <!-- 冻结事实 -->
        <div class="space-y-1.5 rounded border bg-muted/30 p-3 font-mono text-[11px]">
          <div>状态: {{ selectedPrep.status }} | 风险等级: {{ selectedPrep.risk_level }}</div>
          <div>输入指纹: {{ selectedPrep.input_fingerprint }}</div>
          <div>制品 SHA256: {{ selectedPrep.artifact_sha256 }}</div>
          <div>Metadata Snapshot: {{ selectedPrep.metadata_snapshot_id }}</div>
          <div>资源范围: {{ (selectedPrep.resource_scope || []).join(', ') || '-' }}</div>
          <div>数据分级: {{ selectedPrep.data_classification || 'CONFIDENTIAL' }}</div>
          <div>Secret 引用: {{ (selectedPrep.secret_refs || []).join(', ') || '-' }}</div>
          <div>预算摘要: {{ JSON.stringify(selectedPrep.budget_summary) }}</div>
          <div>回滚方案: {{ selectedPrep.rollback_plan }}</div>
        </div>

        <!-- 审批意见输入 -->
        <div>
          <label class="mb-1 block font-medium">审批 / 拒绝理由</label>
          <input
            v-model="approvalReason"
            placeholder="填写审查结论（如：Schema、脱敏与预算均符合 P1 要求）"
            class="w-full rounded border bg-background px-2.5 py-1.5"
          />
        </div>

        <!-- 独立双审批槽位 -->
        <div class="space-y-2">
          <div class="font-semibold">独立职责审批槽位 (SoD Enforced)</div>
          <div
            v-for="slot in selectedPrep.approval_slots"
            :key="slot.slot_type"
            class="rounded border p-3"
          >
            <div class="flex items-center justify-between">
              <span class="font-semibold">{{ slot.slot_type }}</span>
              <Badge
                :variant="
                  slot.status === 'APPROVED'
                    ? 'success'
                    : slot.status === 'REJECTED'
                      ? 'danger'
                      : 'warning'
                "
              >
                {{ slot.status }}
              </Badge>
            </div>
            <div v-if="slot.decided_by_name" class="mt-1 text-[11px] text-muted-foreground">
              决策人: {{ slot.decided_by_name }} ({{ slot.decided_at }}) · 理由: {{ slot.reason }}
            </div>
            <div class="mt-2 flex gap-2">
              <button
                type="button"
                class="rounded bg-emerald-600 px-2.5 py-1 text-white hover:bg-emerald-500"
                @click="handleSubmitApproval(slot.slot_type, 'APPROVED')"
              >
                通过 ({{ slot.slot_type }})
              </button>
              <button
                type="button"
                class="rounded bg-rose-600 px-2.5 py-1 text-white hover:bg-rose-500"
                @click="handleSubmitApproval(slot.slot_type, 'REJECTED')"
              >
                拒绝
              </button>
            </div>
          </div>
        </div>

        <!-- Operator Commit -->
        <div class="border-t pt-3">
          <button
            type="button"
            class="w-full rounded-md bg-primary px-4 py-2 font-medium text-primary-foreground hover:opacity-90"
            @click="openCommitModal"
          >
            Operator Commit（重验指纹并签发单次 Capability）
          </button>
        </div>
      </div>
    </Drawer>

    <!-- Operator Commit 确认弹窗 -->
    <Modal
      :open="commitModalOpen"
      title="确认执行 Operator Commit"
      description="将触发 Tool Broker 重验制品指纹、审批摘要与预算，并原子签发 5 分钟单次 Ed25519 Capability + Outbox。"
      @close="commitModalOpen = false"
    >
      <div class="space-y-3 text-xs">
        <div>
          <label class="mb-1 block font-medium">Idempotency-Key</label>
          <input
            v-model="commitIdempotencyKey"
            class="w-full rounded border bg-background px-2.5 py-1.5 font-mono"
          />
        </div>
        <div class="flex justify-end gap-2">
          <button type="button" class="rounded border px-3 py-1.5" @click="commitModalOpen = false">
            取消
          </button>
          <button
            type="button"
            class="rounded bg-primary px-3 py-1.5 font-medium text-primary-foreground"
            @click="handleConfirmCommit"
          >
            确认 Commit
          </button>
        </div>
      </div>
    </Modal>
  </div>
</template>
