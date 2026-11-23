<!-- 总览工作台：全部 KPI 均由真实 /api/v1/projects/{project_id}/overview 接口驱动 -->
<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { ProjectOverviewResponse, ViewDataState } from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import StateView from '@/components/ui/StateView.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const overview = ref<ProjectOverviewResponse | null>(null)
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

async function loadOverview(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const data = await api.getOverview(projectId)
    overview.value = data
    const hasMissingMetrics =
      data.success_rate === null || data.reject_rate === null || data.budget_utilization_pct === null
    state.value = hasMissingMetrics ? 'partial' : 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'OVERVIEW_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载总览数据失败',
        traceId: 'client-local',
      }
    }
  }
}

onMounted(() => {
  void loadOverview()
})

watch(
  () => auth.activeProject?.id,
  () => {
    void loadOverview()
  },
)
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">总览工作台</h1>
        <p class="text-xs text-muted-foreground">
          当前上下文：{{ auth.activeOrganization?.name || '-' }} /
          {{ auth.activeProject?.name || '-' }} ({{ auth.activeEnvironment.toUpperCase() }})
        </p>
      </div>
      <button
        type="button"
        class="rounded-md border px-3 py-1.5 text-xs hover:bg-accent"
        @click="loadOverview"
      >
        刷新实时指标
      </button>
    </div>

    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      empty-title="请先选择项目上下文"
      empty-description="在顶部栏选择有效的 Organization 与 Project 后，将展示实时控制面统计。"
      partial-notice="部分运行成功率或预算指标暂无样本（尚未产生已完成的 ExecutionRun）。"
      @retry="loadOverview"
    >
      <div v-if="overview" class="space-y-5">
        <!-- 核心 KPI 网格（轻量顺序入场 + tabular-nums 对齐数字） -->
        <div class="stagger-grid grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Card title="数据源连接健康度">
            <div class="text-2xl font-bold font-mono tabular-nums">
              {{ overview.connections_healthy }} / {{ overview.connections_total }}
            </div>
            <p class="mt-1 text-xs text-muted-foreground">已探查可用连接数 / 总连接数</p>
          </Card>

          <Card title="Pipeline 与冻结版本">
            <div class="text-2xl font-bold font-mono tabular-nums">
              {{ overview.pipelines_total }}
              <span class="text-sm font-normal text-muted-foreground">
                ({{ overview.latest_versions_count }} 版本)
              </span>
            </div>
            <p class="mt-1 text-xs text-muted-foreground">不可变 SHA256 ArtifactManifest</p>
          </Card>

          <Card title="Harness 待处理门禁">
            <div class="flex flex-wrap items-center gap-1.5 text-xs font-mono tabular-nums">
              <Badge variant="outline">待 Prepare: {{ overview.pending_preparations }}</Badge>
              <Badge variant="warning">待审批: {{ overview.pending_approvals }}</Badge>
              <Badge variant="info">待 Commit: {{ overview.pending_commits }}</Badge>
            </div>
            <p class="mt-2 text-xs text-muted-foreground">Prepare → Approve → Commit 严格分离</p>
          </Card>

          <Card title="活跃执行任务 (Batch / CDC)">
            <div class="flex flex-wrap items-baseline gap-1 text-2xl font-bold font-mono tabular-nums">
              <span>{{ overview.active_batch_runs }}</span>
              <span class="text-xs font-normal text-muted-foreground">Batch</span>
              <span class="text-muted-foreground/60">/</span>
              <span>{{ overview.active_cdc_runs }}</span>
              <span class="text-xs font-normal text-muted-foreground">CDC</span>
            </div>
            <p class="mt-1 text-xs text-muted-foreground">受控 SeaTunnel Zeta 数据面运行</p>
          </Card>
        </div>

        <!-- 质量与预算指标 -->
        <div class="grid gap-4 md:grid-cols-3">
          <Card title="运行成功率">
            <div class="text-xl font-bold font-mono">
              {{
                overview.success_rate !== null
                  ? `${(overview.success_rate * 100).toFixed(2)}%`
                  : '暂无样本'
              }}
            </div>
          </Card>
          <Card title="整体拒绝率 (error_rows / validated_rows)">
            <div class="text-xl font-bold font-mono">
              {{
                overview.reject_rate !== null
                  ? `${(overview.reject_rate * 100).toFixed(2)}%`
                  : '暂无样本'
              }}
            </div>
            <p class="mt-1 text-xs text-muted-foreground">超过 20% 触发运行监督自动中断</p>
          </Card>
          <Card title="资源预算使用率">
            <div class="text-xl font-bold font-mono">
              {{
                overview.budget_utilization_pct !== null
                  ? `${overview.budget_utilization_pct.toFixed(1)}%`
                  : '暂无样本'
              }}
            </div>
          </Card>
        </div>

        <!-- 新手 5 步快速上手引导（通俗指引，点击任意步骤直达对应工作区） -->
        <Card
          title="新手 5 步上手流程（第一次用按顺序点即可）"
          subtitle="就像企业大额转账：先查账（数据源）→ 填方案（Studio）→ 双主管审批 → 运维打款执行 → 审计留痕"
        >
          <div class="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-5 text-xs">
            <RouterLink
              to="/connections"
              class="group rounded-lg border bg-background/60 p-3 transition-all hover:border-primary/40 hover:bg-primary-soft/40"
            >
              <div class="flex items-center justify-between font-semibold text-foreground">
                <span>① 看数据源</span>
                <Badge variant="outline">Maker</Badge>
              </div>
              <p class="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                探活 MySQL / Doris，点「发起只读 Probe」查看真实表结构与脱敏字段。
              </p>
            </RouterLink>

            <RouterLink
              to="/studio"
              class="group rounded-lg border bg-background/60 p-3 transition-all hover:border-primary/40 hover:bg-primary-soft/40"
            >
              <div class="flex items-center justify-between font-semibold text-foreground">
                <span>② AI 写方案</span>
                <Badge variant="outline">Maker</Badge>
              </div>
              <p class="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                新建 Pipeline，用中文描述同步需求 → 跑门禁 → 冻结版本 → 发起 Prepare。
              </p>
            </RouterLink>

            <RouterLink
              to="/approvals"
              class="group rounded-lg border bg-background/60 p-3 transition-all hover:border-primary/40 hover:bg-primary-soft/40"
            >
              <div class="flex items-center justify-between font-semibold text-foreground">
                <span>③ 双人审批</span>
                <Badge variant="warning">Checker</Badge>
              </div>
              <p class="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                换 data_checker 与 security_checker 账号分别批双槽（发起人不能自批）。
              </p>
            </RouterLink>

            <RouterLink
              to="/executions"
              class="group rounded-lg border bg-background/60 p-3 transition-all hover:border-primary/40 hover:bg-primary-soft/40"
            >
              <div class="flex items-center justify-between font-semibold text-foreground">
                <span>④ 运维执行</span>
                <Badge variant="info">Operator</Badge>
              </div>
              <p class="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                切 operator 账号点 Commit 触发运行，在此实时监控吞吐、质量或发起回滚。
              </p>
            </RouterLink>

            <RouterLink
              to="/users"
              class="group rounded-lg border bg-background/60 p-3 transition-all hover:border-primary/40 hover:bg-primary-soft/40"
            >
              <div class="flex items-center justify-between font-semibold text-foreground">
                <span>⑤ 成员与审计</span>
                <Badge variant="success">Admin</Badge>
              </div>
              <p class="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                管理员在此新增员工、分配角色或禁用账号；去「安全与 Benchmark」验签证据链。
              </p>
            </RouterLink>
          </div>
        </Card>

        <!-- 最近 Evidence 审计事件 -->
        <Card title="最近 Evidence 审计事件" subtitle="只读 SHA256 防篡改账本摘要">
          <div
            v-if="overview.recent_audit_events.length === 0"
            class="text-xs text-muted-foreground"
          >
            暂无审计事件记录。
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead class="border-b text-muted-foreground">
                <tr>
                  <th class="py-2 pr-3">Seq</th>
                  <th class="py-2 pr-3">事件类型</th>
                  <th class="py-2 pr-3">资源</th>
                  <th class="py-2 pr-3">SHA256</th>
                  <th class="py-2">时间</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <tr v-for="ev in overview.recent_audit_events" :key="ev.id">
                  <td class="py-2 pr-3 font-mono">#{{ ev.seq }}</td>
                  <td class="py-2 pr-3 font-medium">{{ ev.event_type }}</td>
                  <td class="py-2 pr-3 font-mono">{{ ev.resource_type }}:{{ ev.resource_id }}</td>
                  <td class="py-2 pr-3 font-mono text-xs text-muted-foreground">
                    {{ (ev.sha256_hash || '').slice(0, 16) }}…
                  </td>
                  <td class="py-2 text-muted-foreground">{{ ev.occurred_at }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </StateView>
  </div>
</template>
