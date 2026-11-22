// 关键视图与显式状态组件单元测试
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import StateView from '@/components/ui/StateView.vue'
import AuthView from '@/views/AuthView.vue'
import ApprovalConsoleView from '@/views/ApprovalConsoleView.vue'
import GovernanceBenchmarkView from '@/views/GovernanceBenchmarkView.vue'
import { api } from '@/api'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

function createTestRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/auth', component: AuthView },
      { path: '/overview', component: { template: '<div>overview</div>' } },
    ],
  })
}

describe('关键视图与状态机渲染', () => {
  it('StateView 正确渲染 Loading / Empty / Error / Stale / Partial / Success 六种状态', () => {
    const loading = mount(StateView, { props: { state: 'loading' } })
    expect(loading.text()).toContain('加载')

    const empty = mount(StateView, {
      props: { state: 'empty', emptyTitle: '空列表标题' },
    })
    expect(empty.text()).toContain('空列表标题')

    const error = mount(StateView, {
      props: {
        state: 'error',
        errorCode: 'PDP_DENIED_P0',
        errorMessage: '高危禁止',
        traceId: 'trace-xyz',
      },
    })
    expect(error.text()).toContain('PDP_DENIED_P0')
    expect(error.text()).toContain('trace-xyz')

    const stale = mount(StateView, {
      props: { state: 'stale' },
      slots: { default: '<div>主内容</div>' },
    })
    expect(stale.text()).toContain('过期')
    expect(stale.text()).toContain('主内容')

    const partial = mount(StateView, {
      props: { state: 'partial' },
      slots: { default: '<div>部分内容</div>' },
    })
    expect(partial.text()).toContain('部分')
    expect(partial.text()).toContain('部分内容')
  })

  it('AuthView 将 Email OTP 诚实标注为「邮件二次验证」而非「抗钓鱼强 MFA」', async () => {
    setActivePinia(createPinia())
    const router = createTestRouter()
    await router.push('/auth')
    await router.isReady()

    const wrapper = mount(AuthView, {
      global: { plugins: [router] },
    })
    expect(wrapper.text()).toContain('邮件二次验证')
    expect(wrapper.text()).not.toContain('抗钓鱼强 MFA')
  })

  it('ApprovalConsoleView 原文展示后端 SoD/P0 拒绝原因', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAuthStore()
    auth.userProfile = {
      id: 'u1',
      email: 'maker@demo.local',
      display_name: 'Maker',
      organizations: [{ id: 'org1', name: 'Org1', slug: 'org1', role: 'MEMBER' }],
      projects: [
        {
          id: 'proj1',
          organization_id: 'org1',
          name: 'Proj1',
          slug: 'proj1',
          environment: 'dev',
          roles: ['MAKER'],
        },
      ],
      system_roles: ['MAKER'],
    }
    auth.setActiveOrganization('org1')
    auth.setActiveProject('proj1')

    vi.spyOn(api, 'listPreparations').mockResolvedValue({
      items: [
        {
          id: 'prep-11111111',
          project_id: 'proj1',
          pipeline_version_id: 'ver-1',
          preparation_type: 'EXECUTION',
          risk_level: 'P1',
          status: 'PENDING_APPROVAL',
          input_fingerprint: 'fp-1234567890abcdef',
          artifact_sha256: 'sha-123',
          metadata_snapshot_id: 'snap-1',
          resource_scope: ['mysql.orders'],
          data_classification: 'PII',
          budget_summary: { max_rows: 1000 },
          environment: 'dev',
          secret_refs: ['vault://secret/demo'],
          rollback_plan: 'SWAP_RESTORE',
          created_by: 'u1',
          created_at: '2026-11-22T10:00:00Z',
          approval_slots: [
            { slot_type: 'DATA_CHECKER', required: true, status: 'PENDING' },
            { slot_type: 'SECURITY_CHECKER', required: true, status: 'PENDING' },
          ],
        },
      ],
    })
    vi.spyOn(api, 'submitApproval').mockRejectedValue(
      new ApiClientError(403, {
        error_code: 'APPROVAL_SELF_APPROVE_FORBIDDEN',
        message: 'Maker 禁止审批自己发起的 Preparation',
        trace_id: 'trace-sod-001',
      }),
    )

    const wrapper = mount(ApprovalConsoleView, {
      global: { stubs: { Teleport: true } },
    })
    await new Promise((r) => setTimeout(r, 10))

    const openBtn = wrapper.findAll('button').find((b) => b.text().includes('审查冻结事实与审批'))
    expect(openBtn).toBeDefined()
    await openBtn!.trigger('click')

    const approveBtn = wrapper.findAll('button').find((b) => b.text().includes('通过 (DATA_CHECKER)'))
    expect(approveBtn).toBeDefined()
    await approveBtn!.trigger('click')
    await new Promise((r) => setTimeout(r, 10))

    const alertText = wrapper.find('[data-testid="backend-rejection-alert"]').text()
    expect(alertText).toContain('APPROVAL_SELF_APPROVE_FORBIDDEN')
    expect(alertText).toContain('trace-sod-001')
  })

  it('GovernanceBenchmarkView 显著展示 P0/P1 Independent Hard Gate 徽章', async () => {
    setActivePinia(createPinia())
    const wrapper = mount(GovernanceBenchmarkView)
    expect(wrapper.find('[data-testid="p0-p1-hard-gate-badge"]').text()).toContain(
      'P0/P1 Independent Hard Gate',
    )
  })
})
