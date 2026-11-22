// Vue Router 路由表与认证守卫
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/overview',
  },
  {
    path: '/auth',
    name: 'Auth',
    component: () => import('@/views/AuthView.vue'),
    meta: { public: true, title: '身份认证与邮件二次验证' },
  },
  {
    path: '/overview',
    name: 'Overview',
    component: () => import('@/views/OverviewView.vue'),
    meta: { title: '总览工作台' },
  },
  {
    path: '/connections',
    name: 'Connections',
    component: () => import('@/views/ConnectionsView.vue'),
    meta: { title: '数据源与资产管理' },
  },
  {
    path: '/studio',
    name: 'PipelineStudio',
    component: () => import('@/views/PipelineStudioView.vue'),
    meta: { title: 'Pipeline Studio' },
  },
  {
    path: '/approvals',
    name: 'ApprovalConsole',
    component: () => import('@/views/ApprovalConsoleView.vue'),
    meta: { title: '审批控制台与 Preparation' },
  },
  {
    path: '/executions',
    name: 'ExecutionCenter',
    component: () => import('@/views/ExecutionCenterView.vue'),
    meta: { title: '运行中心与质量诊断' },
  },
  {
    path: '/governance',
    name: 'GovernanceBenchmark',
    component: () => import('@/views/GovernanceBenchmarkView.vue'),
    meta: { title: '安全治理与 Benchmark' },
  },
]

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory(),
    routes,
  })

  router.beforeEach((to) => {
    const auth = useAuthStore()
    if (!to.meta.public && !auth.isAuthenticated) {
      return { path: '/auth', query: { redirect: to.fullPath } }
    }
    if (to.path === '/auth' && auth.isAuthenticated) {
      return { path: '/overview' }
    }
    return true
  })

  return router
}
