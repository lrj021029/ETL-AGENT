<!-- 企业级控制面主框架（UI 重构 v1）：shadcn-vue Sidebar + Motion 共享布局导航指示器。
     保留原有语义：Org / Project 切换（auth.setActiveOrganization / setActiveProject）、Env 仅前端上下文、角色徽章、退出登录。 -->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { motion } from 'motion-v'
import {
  Activity,
  Check,
  CheckSquare,
  ChevronsUpDown,
  Database,
  LayoutDashboard,
  LogOut,
  Search,
  ShieldCheck,
  Users,
  Workflow,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import type { EnvironmentType } from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Pressable from '@/components/motion/Pressable.vue'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from '@/components/ui/shadcn/sidebar'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/shadcn/breadcrumb'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/shadcn/dropdown-menu'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/shadcn/popover'
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/shadcn/command'
import { Button } from '@/components/ui/shadcn/button'
import { Separator } from '@/components/ui/shadcn/separator'
import { springs } from '@/lib/motion'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

interface NavItem {
  path: string
  label: string
  icon: typeof LayoutDashboard
}

const navGroups: { label: string; items: NavItem[] }[] = [
  {
    label: '工作区',
    items: [
      { path: '/overview', label: '总览', icon: LayoutDashboard },
      { path: '/connections', label: '数据源与资产', icon: Database },
      { path: '/studio', label: 'Pipeline Studio', icon: Workflow },
    ],
  },
  {
    label: '管控',
    items: [
      { path: '/approvals', label: '审批控制台', icon: CheckSquare },
      { path: '/executions', label: '运行与质量', icon: Activity },
      { path: '/governance', label: '安全与 Benchmark', icon: ShieldCheck },
      { path: '/users', label: '成员与账号管理', icon: Users },
    ],
  },
]
const allNav = navGroups.flatMap((g) => g.items)
const envOptions: EnvironmentType[] = ['dev', 'staging', 'prod']

const currentTitle = computed(
  () => allNav.find((n) => n.path === route.path)?.label ?? (route.meta.title as string | undefined) ?? '',
)

const sidebarOpen = computed({
  get: () => !ui.sidebarCollapsed,
  set: (open: boolean) => ui.setSidebarCollapsed(!open),
})

// Org / Project 切换：与原两个下拉框语义一致——先切组织（可能自动选中该组织首个项目），再切项目
const projectPickerOpen = ref(false)
const projectsByOrg = computed(() =>
  auth.organizations.map((org) => ({
    org,
    projects: (auth.userProfile?.projects ?? []).filter((p) => p.organization_id === org.id),
  })),
)

function selectProject(orgId: string, projectId: string): void {
  if (auth.activeOrganization?.id !== orgId) {
    auth.setActiveOrganization(orgId)
  }
  auth.setActiveProject(projectId)
  projectPickerOpen.value = false
}

// Env 仅为前端上下文（TBD-11）：不写入请求头，只作为新建连接的 environment 字段
const envModel = computed({
  get: () => auth.activeEnvironment as string,
  set: (value: string) => auth.setActiveEnvironment(value as EnvironmentType),
})

function envVariant(env: string): 'info' | 'warning' | 'danger' {
  if (env === 'prod') return 'danger'
  if (env === 'staging') return 'warning'
  return 'info'
}

function roleBadgeVariant(role: string): 'danger' | 'warning' | 'info' | 'default' {
  if (role.includes('SECURITY')) return 'danger'
  if (role.includes('DATA_CHECKER')) return 'warning'
  if (role.includes('OPERATOR')) return 'info'
  return 'default'
}

const displayName = computed(() => auth.userProfile?.display_name || auth.userProfile?.email || '未登录')
const userInitial = computed(() => displayName.value.slice(0, 1).toUpperCase())

// 页面跳转命令面板（Ctrl/⌘ + K）；资源级全局搜索 TBD-15
const commandOpen = ref(false)
const routeNavigating = ref(false)
let removeBeforeHook: (() => void) | null = null
let removeAfterHook: (() => void) | null = null

function goTo(path: string): void {
  commandOpen.value = false
  void router.push(path)
}
function onGlobalKeydown(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    commandOpen.value = !commandOpen.value
  }
}
onMounted(() => {
  window.addEventListener('keydown', onGlobalKeydown)
  removeBeforeHook = router.beforeEach((to, from) => {
    if (to.path !== from.path) routeNavigating.value = true
  })
  removeAfterHook = router.afterEach(() => {
    routeNavigating.value = false
  })
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
  removeBeforeHook?.()
  removeAfterHook?.()
})

async function handleLogout(): Promise<void> {
  await auth.logout()
  await router.push('/auth')
}
</script>

<template>
  <SidebarProvider v-model:open="sidebarOpen">
    <Sidebar collapsible="icon" aria-label="主导航">
      <SidebarHeader>
        <div class="flex h-10 items-center gap-2.5 px-1">
          <div
            class="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-[13px] font-bold text-primary-foreground"
          >
            EA
          </div>
          <span class="truncate font-semibold tracking-tight group-data-[collapsible=icon]:hidden">ETL-Agent</span>
        </div>

        <SidebarMenu>
          <SidebarMenuItem>
            <Popover v-model:open="projectPickerOpen">
              <PopoverTrigger as-child>
                <SidebarMenuButton
                  size="lg"
                  tooltip="切换组织 / 项目"
                  aria-label="Project Switcher"
                  class="border border-sidebar-border bg-background"
                >
                  <div
                    class="grid size-8 shrink-0 place-items-center rounded-md bg-primary-soft text-sm font-semibold text-primary"
                  >
                    {{ (auth.activeProject?.name || 'P').slice(0, 1) }}
                  </div>
                  <div class="grid flex-1 text-left leading-tight">
                    <span class="truncate text-[13px] font-semibold">{{ auth.activeProject?.name || '未选择项目' }}</span>
                    <span class="truncate text-xs text-muted-foreground">{{ auth.activeOrganization?.name || '未选择组织' }}</span>
                  </div>
                  <ChevronsUpDown class="ml-auto size-4 text-muted-foreground" />
                </SidebarMenuButton>
              </PopoverTrigger>
              <PopoverContent class="w-72 p-0" align="start">
                <Command>
                  <CommandInput placeholder="搜索组织或项目" />
                  <CommandList>
                    <CommandEmpty>没有匹配的项目</CommandEmpty>
                    <CommandGroup v-for="group in projectsByOrg" :key="group.org.id" :heading="group.org.name">
                      <CommandItem
                        v-for="proj in group.projects"
                        :key="proj.id"
                        :value="`${group.org.name} ${proj.name}`"
                        @select="selectProject(group.org.id, proj.id)"
                      >
                        <Check
                          class="size-4 text-primary"
                          :class="proj.id === auth.activeProject?.id ? 'opacity-100' : 'opacity-0'"
                        />
                        <span class="flex-1 truncate">{{ proj.name }}</span>
                        <Badge :variant="envVariant(proj.environment)">{{ proj.environment }}</Badge>
                      </CommandItem>
                    </CommandGroup>
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup v-for="group in navGroups" :key="group.label">
          <SidebarGroupLabel>{{ group.label }}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem v-for="item in group.items" :key="item.path">
                <SidebarMenuButton
                  as-child
                  :is-active="route.path === item.path"
                  :tooltip="item.label"
                  class="relative isolate transition-transform active:scale-[0.98] data-[active=true]:bg-transparent data-[active=true]:font-semibold data-[active=true]:text-sidebar-accent-foreground"
                >
                  <RouterLink :to="item.path">
                    <!-- 共享布局动画：当前页指示器在导航项之间以弹簧过渡 -->
                    <motion.span
                      v-if="route.path === item.path"
                      layout-id="nav-active-indicator"
                      class="absolute inset-0 -z-10 rounded-md bg-sidebar-accent"
                      :transition="springs.snappy"
                    />
                    <component :is="item.icon" />
                    <span>{{ item.label }}</span>
                  </RouterLink>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger as-child>
                <SidebarMenuButton size="lg" tooltip="账户与角色">
                  <div class="grid size-8 shrink-0 place-items-center rounded-full bg-muted text-xs font-semibold">
                    {{ userInitial }}
                  </div>
                  <div class="grid flex-1 text-left leading-tight">
                    <span class="truncate text-[13px] font-semibold">{{ displayName }}</span>
                    <span class="truncate text-xs text-muted-foreground">
                      {{ auth.activeRoleBadges.join(' · ') || '无角色' }}
                    </span>
                  </div>
                  <ChevronsUpDown class="ml-auto size-4 text-muted-foreground" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="top" align="start" class="w-64">
                <DropdownMenuLabel class="text-xs font-normal text-muted-foreground">
                  {{ auth.userProfile?.email || '未登录' }}
                </DropdownMenuLabel>
                <div class="flex flex-wrap gap-1 px-2 pb-2">
                  <Badge v-for="role in auth.activeRoleBadges" :key="role" :variant="roleBadgeVariant(role)">
                    {{ role }}
                  </Badge>
                </div>
                <DropdownMenuSeparator />
                <DropdownMenuItem @select="handleLogout">
                  <LogOut class="size-4" />
                  退出登录
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>

    <SidebarInset class="min-w-0 bg-background">
      <header
        class="sticky top-0 z-30 flex h-[60px] shrink-0 items-center gap-3 border-b bg-card/90 px-4 backdrop-blur lg:px-6"
      >
        <!-- 顶部 2px 路由切换微光进度条，为异步页面 chunk 加载提供即时反馈 -->
        <div
          v-if="routeNavigating"
          aria-hidden="true"
          class="pointer-events-none absolute inset-x-0 bottom-0 h-[2px] overflow-hidden bg-primary/15"
        >
          <div class="route-progress-bar h-full w-1/3 bg-primary" />
        </div>
        <SidebarTrigger aria-label="折叠侧栏" />
        <Separator orientation="vertical" class="!h-5" />
        <Breadcrumb>
          <BreadcrumbList>
            <BreadcrumbItem class="hidden md:inline-flex">
              {{ auth.activeProject?.name || '未选择项目' }}
            </BreadcrumbItem>
            <BreadcrumbSeparator class="hidden md:inline-flex" />
            <BreadcrumbItem>
              <Transition name="badge-pop" mode="out-in">
                <BreadcrumbPage :key="currentTitle" class="font-semibold">{{ currentTitle }}</BreadcrumbPage>
              </Transition>
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>

        <div class="flex-1" />

        <DropdownMenu>
          <DropdownMenuTrigger as-child>
            <Button variant="outline" size="sm" aria-label="Environment Switcher" class="gap-2 transition-transform active:scale-[0.98]">
              <Transition name="badge-pop" mode="out-in">
                <Badge :key="auth.activeEnvironment" :variant="envVariant(auth.activeEnvironment)">
                  {{ auth.activeEnvironment }}
                </Badge>
              </Transition>
              <span class="text-xs text-muted-foreground">环境</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" class="w-72">
            <DropdownMenuLabel>环境上下文</DropdownMenuLabel>
            <p class="px-2 pb-2 text-xs text-muted-foreground">
              仅前端状态：不写入请求头，只作为新建连接的 environment 字段；切换项目时自动设为项目环境。
            </p>
            <DropdownMenuSeparator />
            <DropdownMenuRadioGroup v-model="envModel">
              <DropdownMenuRadioItem v-for="env in envOptions" :key="env" :value="env" class="font-mono uppercase">
                {{ env }}
              </DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>

        <Pressable>
          <Button
            variant="outline"
            size="sm"
            class="hidden w-56 justify-start gap-2 font-normal text-muted-foreground sm:inline-flex"
            @click="commandOpen = true"
          >
            <Search class="size-4" />
            跳转到页面…
            <kbd class="ml-auto rounded border border-b-2 px-1.5 font-mono text-[11px] leading-4">Ctrl K</kbd>
          </Button>
        </Pressable>
      </header>

      <main class="min-w-0 flex-1 px-4 py-6 lg:px-8">
        <slot />
      </main>
    </SidebarInset>

    <CommandDialog v-model:open="commandOpen">
      <CommandInput placeholder="输入页面名称" />
      <CommandList>
        <CommandEmpty>没有匹配的页面</CommandEmpty>
        <CommandGroup heading="页面">
          <CommandItem v-for="item in allNav" :key="item.path" :value="item.label" @select="goTo(item.path)">
            <component :is="item.icon" class="size-4" />
            {{ item.label }}
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  </SidebarProvider>
</template>
