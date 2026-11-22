<!-- 企业级控制面主框架：包含 Organization / Project / Environment 切换器、当前角色徽章、主题切换与导航侧栏 -->
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity,
  Database,
  LayoutDashboard,
  LogOut,
  Moon,
  ShieldCheck,
  Sun,
  Workflow,
  CheckSquare,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import Badge from '@/components/ui/Badge.vue'
import type { EnvironmentType } from '@/types/api'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

const navItems = [
  { path: '/overview', label: '总览工作台', icon: LayoutDashboard },
  { path: '/connections', label: '数据源与资产', icon: Database },
  { path: '/studio', label: 'Pipeline Studio', icon: Workflow },
  { path: '/approvals', label: '审批控制台', icon: CheckSquare },
  { path: '/executions', label: '运行与质量中心', icon: Activity },
  { path: '/governance', label: '安全治理与 Benchmark', icon: ShieldCheck },
]

const envOptions: EnvironmentType[] = ['dev', 'staging', 'prod']

const roleBadgeVariant = computed(() => (role: string) => {
  if (role.includes('SECURITY')) return 'danger'
  if (role.includes('DATA_CHECKER')) return 'warning'
  if (role.includes('OPERATOR')) return 'info'
  return 'default'
})

async function handleLogout(): Promise<void> {
  await auth.logout()
  await router.push('/auth')
}
</script>

<template>
  <div class="flex min-h-screen bg-background text-foreground">
    <!-- 左侧导航栏 -->
    <aside
      class="flex shrink-0 flex-col border-r bg-card transition-all"
      :class="ui.sidebarCollapsed ? 'w-16' : 'w-60'"
    >
      <div class="flex h-14 items-center justify-between border-b px-4">
        <span v-if="!ui.sidebarCollapsed" class="font-mono text-sm font-bold tracking-wider">
          ETL-AGENT
        </span>
        <button
          type="button"
          class="rounded border px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-accent"
          @click="ui.toggleSidebar()"
        >
          {{ ui.sidebarCollapsed ? '»' : '«' }}
        </button>
      </div>

      <nav class="flex-1 space-y-1 p-2">
        <RouterLink
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          class="flex items-center gap-3 rounded-md px-3 py-2 text-xs font-medium transition-colors"
          :class="
            route.path === item.path
              ? 'bg-primary/15 text-primary'
              : 'text-muted-foreground hover:bg-accent hover:text-foreground'
          "
        >
          <component :is="item.icon" class="h-4 w-4 shrink-0" />
          <span v-if="!ui.sidebarCollapsed">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div v-if="!ui.sidebarCollapsed" class="border-t p-3 text-[11px] text-muted-foreground">
        <div>Control / Data Plane 分离</div>
        <div class="font-mono">SeaTunnel Zeta 受管执行</div>
      </div>
    </aside>

    <!-- 右侧主工作区 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <!-- 顶部租户上下文与用户身份栏 -->
      <header class="flex h-14 flex-wrap items-center justify-between gap-3 border-b bg-card px-5">
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <label class="flex items-center gap-1.5">
            <span class="text-muted-foreground">Org:</span>
            <select
              aria-label="Organization Switcher"
              class="rounded border bg-background px-2 py-1 text-xs"
              :value="auth.activeOrganization?.id || ''"
              @change="auth.setActiveOrganization(($event.target as HTMLSelectElement).value)"
            >
              <option v-if="auth.organizations.length === 0" value="">未选择组织</option>
              <option v-for="org in auth.organizations" :key="org.id" :value="org.id">
                {{ org.name }}
              </option>
            </select>
          </label>

          <label class="flex items-center gap-1.5">
            <span class="text-muted-foreground">Project:</span>
            <select
              aria-label="Project Switcher"
              class="rounded border bg-background px-2 py-1 text-xs"
              :value="auth.activeProject?.id || ''"
              @change="auth.setActiveProject(($event.target as HTMLSelectElement).value)"
            >
              <option v-if="auth.projects.length === 0" value="">未选择项目</option>
              <option v-for="proj in auth.projects" :key="proj.id" :value="proj.id">
                {{ proj.name }}
              </option>
            </select>
          </label>

          <label class="flex items-center gap-1.5">
            <span class="text-muted-foreground">Env:</span>
            <select
              aria-label="Environment Switcher"
              class="rounded border bg-background px-2 py-1 font-mono text-xs uppercase"
              :value="auth.activeEnvironment"
              @change="
                auth.setActiveEnvironment(
                  ($event.target as HTMLSelectElement).value as EnvironmentType,
                )
              "
            >
              <option v-for="env in envOptions" :key="env" :value="env">{{ env }}</option>
            </select>
          </label>
        </div>

        <div class="flex items-center gap-2">
          <div class="flex flex-wrap items-center gap-1">
            <Badge
              v-for="role in auth.activeRoleBadges"
              :key="role"
              :variant="roleBadgeVariant(role)"
            >
              {{ role }}
            </Badge>
          </div>

          <span class="text-xs text-muted-foreground">
            {{ auth.userProfile?.display_name || auth.userProfile?.email || '未登录' }}
          </span>

          <button
            type="button"
            aria-label="Toggle Theme"
            class="rounded-md border p-1.5 hover:bg-accent"
            @click="ui.toggleTheme()"
          >
            <Sun v-if="ui.isDark" class="h-3.5 w-3.5" />
            <Moon v-else class="h-3.5 w-3.5" />
          </button>

          <button
            type="button"
            class="flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-accent"
            @click="handleLogout"
          >
            <LogOut class="h-3.5 w-3.5" />
            退出
          </button>
        </div>
      </header>

      <main class="flex-1 overflow-y-auto p-5">
        <slot />
      </main>
    </div>
  </div>
</template>
