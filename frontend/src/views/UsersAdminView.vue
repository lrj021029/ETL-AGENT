<!-- 企业成员与账号管理：管理员新增员工、分配/调整 RBAC 角色与启停账号；普通成员只读查看团队分工 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ShieldCheck, UserPlus } from 'lucide-vue-next'
import { api } from '@/api'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type { AdminUserItem, SystemRole, ViewDataState } from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import Modal from '@/components/ui/Modal.vue'
import StateView from '@/components/ui/StateView.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)
const users = ref<AdminUserItem[]>([])

const actionBanner = ref<{ kind: 'success' | 'error'; text: string } | null>(null)
const createModalOpen = ref(false)
const submitting = ref(false)

const newUser = ref({
  display_name: '',
  email: '',
  password: '',
  org_role: 'MEMBER' as 'MEMBER' | 'ORG_ADMIN',
  project_role_code: 'MAKER' as SystemRole,
})

const systemRoleOptions: Array<{ code: SystemRole; label: string }> = [
  { code: 'MAKER', label: 'MAKER · 研发发起人' },
  { code: 'DATA_CHECKER', label: 'DATA_CHECKER · 数据审批员' },
  { code: 'SECURITY_CHECKER', label: 'SECURITY_CHECKER · 安全审批员' },
  { code: 'OPERATOR', label: 'OPERATOR · 运维执行员' },
  { code: 'AUDITOR', label: 'AUDITOR · 只读审计员' },
  { code: 'PROJECT_ADMIN', label: 'PROJECT_ADMIN · 项目管理员' },
  { code: 'ORG_ADMIN', label: 'ORG_ADMIN · 组织管理员' },
]

const isCurrentUserAdmin = computed(() => {
  const roles = auth.activeRoleBadges
  return (
    auth.activeOrganization?.role === 'ORG_ADMIN' ||
    roles.includes('ORG_ADMIN') ||
    roles.includes('PROJECT_ADMIN')
  )
})

function roleBadgeVariant(role: string): 'danger' | 'warning' | 'info' | 'success' | 'outline' {
  if (role.includes('SECURITY') || role === 'ORG_ADMIN') return 'danger'
  if (role.includes('DATA_CHECKER') || role === 'PROJECT_ADMIN') return 'warning'
  if (role === 'OPERATOR') return 'info'
  if (role === 'AUDITOR') return 'success'
  return 'outline'
}

async function loadUsers(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const res = await api.listAdminUsers(projectId)
    users.value = res.items || []
    state.value = users.value.length === 0 ? 'empty' : 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'USERS_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载成员列表失败',
        traceId: 'client-local',
      }
    }
  }
}

async function handleCreateUser(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  submitting.value = true
  actionBanner.value = null
  try {
    const created = await api.createAdminUser(projectId, {
      display_name: newUser.value.display_name.trim(),
      email: newUser.value.email.trim(),
      password: newUser.value.password,
      org_role: newUser.value.org_role,
      project_role_code: newUser.value.project_role_code,
    })
    createModalOpen.value = false
    newUser.value = {
      display_name: '',
      email: '',
      password: '',
      org_role: 'MEMBER',
      project_role_code: 'MAKER',
    }
    actionBanner.value = {
      kind: 'success',
      text: `已新增员工账号「${created.display_name}」(${created.email})，角色：${created.project_roles.join(', ')}`,
    }
    await loadUsers()
  } catch (err) {
    actionBanner.value = {
      kind: 'error',
      text:
        err instanceof ApiClientError
          ? `${err.errorCode}: ${err.message} (Trace: ${err.traceId})`
          : String(err),
    }
  } finally {
    submitting.value = false
  }
}

async function handleToggleStatus(user: AdminUserItem): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  actionBanner.value = null
  const nextStatus = user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    await api.updateAdminUserStatus(projectId, user.id, nextStatus)
    actionBanner.value = {
      kind: 'success',
      text: `已将员工「${user.display_name}」账号状态更新为 ${nextStatus}`,
    }
    await loadUsers()
  } catch (err) {
    actionBanner.value = {
      kind: 'error',
      text:
        err instanceof ApiClientError
          ? `${err.errorCode}: ${err.message} (Trace: ${err.traceId})`
          : String(err),
    }
  }
}

async function handleChangePrimaryRole(user: AdminUserItem, nextRole: string): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !nextRole) return
  actionBanner.value = null
  try {
    await api.updateAdminUserRoles(projectId, user.id, {
      org_role: nextRole === 'ORG_ADMIN' ? 'ORG_ADMIN' : 'MEMBER',
      project_roles: [nextRole],
    })
    actionBanner.value = {
      kind: 'success',
      text: `已调整「${user.display_name}」的项目角色为 ${nextRole}`,
    }
    await loadUsers()
  } catch (err) {
    actionBanner.value = {
      kind: 'error',
      text:
        err instanceof ApiClientError
          ? `${err.errorCode}: ${err.message} (Trace: ${err.traceId})`
          : String(err),
    }
  }
}

onMounted(() => {
  void loadUsers()
})

watch(
  () => auth.activeProject?.id,
  () => {
    void loadUsers()
  },
)
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">企业成员与账号管理</h1>
        <p class="text-xs text-muted-foreground">
          支持新员工在登录页自助注册（默认最小权限 MAKER），并由管理员在此统一新增账号、分配职责分离角色与启停状态
        </p>
      </div>
      <div class="flex items-center gap-2 text-xs">
        <Badge :variant="isCurrentUserAdmin ? 'success' : 'outline'">
          <ShieldCheck class="mr-1 inline size-3.5" />
          {{ isCurrentUserAdmin ? '当前具备管理员写权限' : '当前为只读花名册视角（写操作需 Admin）' }}
        </Badge>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground transition-transform active:scale-[0.98] hover:opacity-90"
          @click="createModalOpen = true"
        >
          <UserPlus class="size-3.5" />
          新增员工账号
        </button>
      </div>
    </div>

    <div
      v-if="actionBanner"
      role="status"
      class="rounded-md border px-3.5 py-2.5 text-xs"
      :class="
        actionBanner.kind === 'success'
          ? 'border-success/35 bg-success-soft text-success-text'
          : 'border-destructive/35 bg-destructive-soft text-destructive-text'
      "
    >
      {{ actionBanner.text }}
    </div>

    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      empty-title="当前组织暂无成员"
      empty-description="点击右上角「新增员工账号」或引导员工在登录页完成自助注册。"
      @retry="loadUsers"
    >
      <Card
        :title="`组织成员花名册 (${users.length} 人)`"
        :subtitle="`当前组织：${auth.activeOrganization?.name || '-'} · 当前项目：${auth.activeProject?.name || '-'}`"
      >
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="border-b text-muted-foreground">
              <tr>
                <th class="py-2 pr-3">姓名 / 邮箱</th>
                <th class="py-2 pr-3">账号状态</th>
                <th class="py-2 pr-3">组织角色</th>
                <th class="py-2 pr-3">当前项目角色（RBAC）</th>
                <th class="py-2 text-right">管理员操作</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="u in users" :key="u.id" class="hover:bg-muted/40">
                <td class="py-2.5 pr-3">
                  <div class="font-medium text-foreground">{{ u.display_name }}</div>
                  <div class="font-mono text-[11px] text-muted-foreground">{{ u.email }}</div>
                </td>
                <td class="py-2.5 pr-3">
                  <Badge :variant="u.status === 'ACTIVE' ? 'success' : 'danger'">
                    {{ u.status === 'ACTIVE' ? '已激活 ACTIVE' : '已禁用 DISABLED' }}
                  </Badge>
                </td>
                <td class="py-2.5 pr-3 font-mono">
                  <Badge :variant="u.org_role === 'ORG_ADMIN' ? 'danger' : 'outline'">
                    {{ u.org_role }}
                  </Badge>
                </td>
                <td class="py-2.5 pr-3">
                  <div class="flex flex-wrap gap-1">
                    <Badge
                      v-for="r in u.project_roles"
                      :key="r"
                      :variant="roleBadgeVariant(r)"
                    >
                      {{ r }}
                    </Badge>
                    <span v-if="u.project_roles.length === 0" class="text-muted-foreground">未加入本项目</span>
                  </div>
                </td>
                <td class="py-2.5 text-right">
                  <div class="inline-flex flex-wrap items-center justify-end gap-2">
                    <select
                      :value="u.project_roles[0] || 'MAKER'"
                      aria-label="调整员工角色"
                      class="rounded border bg-background px-2 py-1 text-xs"
                      @change="handleChangePrimaryRole(u, ($event.target as HTMLSelectElement).value)"
                    >
                      <option v-for="opt in systemRoleOptions" :key="opt.code" :value="opt.code">
                        {{ opt.code }}
                      </option>
                    </select>
                    <button
                      type="button"
                      class="rounded border px-2.5 py-1 text-xs transition-colors"
                      :class="
                        u.status === 'ACTIVE'
                          ? 'border-destructive/35 text-destructive-text hover:bg-destructive-soft'
                          : 'border-success/35 text-success-text hover:bg-success-soft'
                      "
                      @click="handleToggleStatus(u)"
                    >
                      {{ u.status === 'ACTIVE' ? '禁用账号' : '重新启用' }}
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>
    </StateView>

    <!-- 新增企业员工弹窗 -->
    <Modal
      :open="createModalOpen"
      title="新增企业员工账号"
      description="仅组织管理员 (ORG_ADMIN) 或项目管理员 (PROJECT_ADMIN) 可提交；非管理员提交将被后端权限守卫明确拦截。"
      @close="createModalOpen = false"
    >
      <form class="space-y-3 text-xs" @submit.prevent="handleCreateUser">
        <div>
          <label class="mb-1 block font-medium">员工姓名</label>
          <input
            v-model="newUser.display_name"
            required
            minlength="2"
            placeholder="例如：李四（数据审批）"
            class="w-full rounded border bg-background px-2.5 py-1.5"
          />
        </div>
        <div>
          <label class="mb-1 block font-medium">企业邮箱</label>
          <input
            v-model="newUser.email"
            type="email"
            required
            placeholder="lisi@etl-agent.local"
            class="w-full rounded border bg-background px-2.5 py-1.5"
          />
        </div>
        <div>
          <label class="mb-1 block font-medium">初始登录密码</label>
          <input
            v-model="newUser.password"
            type="password"
            required
            minlength="8"
            placeholder="至少 8 位且包含字母与数字"
            class="w-full rounded border bg-background px-2.5 py-1.5"
          />
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="mb-1 block font-medium">组织角色</label>
            <select v-model="newUser.org_role" class="w-full rounded border bg-background px-2 py-1.5">
              <option value="MEMBER">MEMBER（普通成员）</option>
              <option value="ORG_ADMIN">ORG_ADMIN（组织管理员）</option>
            </select>
          </div>
          <div>
            <label class="mb-1 block font-medium">项目职能角色</label>
            <select v-model="newUser.project_role_code" class="w-full rounded border bg-background px-2 py-1.5">
              <option v-for="opt in systemRoleOptions" :key="opt.code" :value="opt.code">
                {{ opt.label }}
              </option>
            </select>
          </div>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="rounded border px-3 py-1.5" @click="createModalOpen = false">
            取消
          </button>
          <button
            type="submit"
            :disabled="submitting"
            class="rounded bg-primary px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-50"
          >
            {{ submitting ? '创建中…' : '确认创建员工' }}
          </button>
        </div>
      </form>
    </Modal>
  </div>
</template>
