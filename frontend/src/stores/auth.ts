// 认证与租户上下文 Store：管理 JWT 会话、邮件二次验证流程、Organization / Project / Environment 切换与角色徽章
// 严格约束：绝不在浏览器存储中保存数据库密码、LLM API Key 或 Capability 私钥
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type {
  EnvironmentType,
  OrganizationItem,
  OtpChallengeResponse,
  ProjectItem,
  UserProfile,
} from '@/types/api'
import { api } from '@/api'

const SESSION_TOKEN_KEY = 'etl_agent_session_jwt'
const SESSION_ORG_KEY = 'etl_agent_active_org'
const SESSION_PROJECT_KEY = 'etl_agent_active_project'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(
    typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(SESSION_TOKEN_KEY) : null,
  )
  const refreshToken = ref<string | null>(null)
  const activeOrgId = ref<string | null>(
    typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(SESSION_ORG_KEY) : null,
  )
  const activeProjectId = ref<string | null>(
    typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(SESSION_PROJECT_KEY) : null,
  )
  const activeEnvironment = ref<EnvironmentType>('dev')
  const userProfile = ref<UserProfile | null>(null)
  const otpChallenge = ref<OtpChallengeResponse | null>(null)

  const isAuthenticated = computed(() => Boolean(accessToken.value))

  const organizations = computed<OrganizationItem[]>(() => userProfile.value?.organizations ?? [])
  const projects = computed<ProjectItem[]>(() => {
    const all = userProfile.value?.projects ?? []
    if (!activeOrgId.value) return all
    return all.filter((p) => p.organization_id === activeOrgId.value)
  })

  const activeOrganization = computed(
    () => organizations.value.find((o) => o.id === activeOrgId.value) ?? organizations.value[0] ?? null,
  )

  const activeProject = computed(
    () => projects.value.find((p) => p.id === activeProjectId.value) ?? projects.value[0] ?? null,
  )

  const activeRoleBadges = computed<string[]>(() => {
    const sys = userProfile.value?.system_roles ?? []
    const proj = activeProject.value?.roles ?? []
    return Array.from(new Set([...sys, ...proj]))
  })

  function setActiveOrganization(orgId: string): void {
    activeOrgId.value = orgId
    if (typeof sessionStorage !== 'undefined') {
      sessionStorage.setItem(SESSION_ORG_KEY, orgId)
    }
    const matchingProjects = (userProfile.value?.projects ?? []).filter(
      (p) => p.organization_id === orgId,
    )
    if (matchingProjects.length > 0 && !matchingProjects.some((p) => p.id === activeProjectId.value)) {
      setActiveProject(matchingProjects[0].id)
    }
  }

  function setActiveProject(projectId: string): void {
    activeProjectId.value = projectId
    if (typeof sessionStorage !== 'undefined') {
      sessionStorage.setItem(SESSION_PROJECT_KEY, projectId)
    }
    const found = (userProfile.value?.projects ?? []).find((p) => p.id === projectId)
    if (found) {
      activeEnvironment.value = found.environment
    }
  }

  function setActiveEnvironment(env: EnvironmentType): void {
    activeEnvironment.value = env
  }

  async function loginWithPassword(email: string, password: string): Promise<OtpChallengeResponse> {
    const challenge = await api.login({ email, password })
    otpChallenge.value = challenge
    return challenge
  }

  async function verifyEmailOtp(otpCode: string): Promise<void> {
    if (!otpChallenge.value) {
      throw new Error('缺少邮件二次验证挑战上下文')
    }
    const tokens = await api.verifyOtp({
      challenge_id: otpChallenge.value.challenge_id,
      otp_code: otpCode,
    })
    accessToken.value = tokens.access_token
    refreshToken.value = tokens.refresh_token
    if (typeof sessionStorage !== 'undefined') {
      sessionStorage.setItem(SESSION_TOKEN_KEY, tokens.access_token)
    }
    otpChallenge.value = null
    await fetchCurrentUser()
  }

  async function fetchCurrentUser(): Promise<UserProfile | null> {
    if (!accessToken.value) return null
    const profile = await api.getMe()
    userProfile.value = profile
    if (!activeOrgId.value && profile.organizations.length > 0) {
      setActiveOrganization(profile.organizations[0].id)
    }
    if (!activeProjectId.value && profile.projects.length > 0) {
      const firstInOrg =
        profile.projects.find((p) => p.organization_id === activeOrgId.value) ?? profile.projects[0]
      setActiveProject(firstInOrg.id)
    }
    return profile
  }

  async function logout(): Promise<void> {
    try {
      if (accessToken.value) {
        await api.logout()
      }
    } catch {
      // 登出接口异常时仍清理本地会话状态
    } finally {
      accessToken.value = null
      refreshToken.value = null
      userProfile.value = null
      otpChallenge.value = null
      if (typeof sessionStorage !== 'undefined') {
        sessionStorage.removeItem(SESSION_TOKEN_KEY)
      }
    }
  }

  return {
    accessToken,
    refreshToken,
    activeOrgId,
    activeProjectId,
    activeEnvironment,
    userProfile,
    otpChallenge,
    isAuthenticated,
    organizations,
    projects,
    activeOrganization,
    activeProject,
    activeRoleBadges,
    setActiveOrganization,
    setActiveProject,
    setActiveEnvironment,
    loginWithPassword,
    verifyEmailOtp,
    fetchCurrentUser,
    logout,
  }
})
