// API 客户端单例导出，动态读取 Pinia Auth Store 中的 Token 与组织/项目上下文
import { ApiClient } from './client'
import { createApiEndpoints } from './endpoints'

const SESSION_TOKEN_KEY = 'etl_agent_session_jwt'
const SESSION_ORG_KEY = 'etl_agent_active_org'
const SESSION_PROJECT_KEY = 'etl_agent_active_project'

export const apiClient = new ApiClient({
  getAccessToken: () =>
    typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(SESSION_TOKEN_KEY) : null,
  getOrganizationId: () =>
    typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(SESSION_ORG_KEY) : null,
  getProjectId: () =>
    typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(SESSION_PROJECT_KEY) : null,
})

export const api = createApiEndpoints(apiClient)
