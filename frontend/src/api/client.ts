// 控制面类型化 HTTP 客户端，自动注入 Bearer Token、租户上下文头与幂等键，并解析标准 ErrorResponse
import type { ErrorResponse } from '@/types/api'

/** API 错误异常类，完整保留后端返回的 error_code、detail 与 trace_id */
export class ApiClientError extends Error {
  readonly status: number
  readonly errorCode: string
  readonly detail: Record<string, unknown> | unknown[] | null
  readonly traceId: string

  constructor(status: number, payload: ErrorResponse) {
    super(payload.message || payload.error_code)
    this.name = 'ApiClientError'
    this.status = status
    this.errorCode = payload.error_code
    this.detail = payload.detail ?? null
    this.traceId = payload.trace_id
  }
}

export interface ApiContextProvider {
  getAccessToken: () => string | null
  getOrganizationId: () => string | null
  getProjectId: () => string | null
}

export interface RequestOptions extends Omit<RequestInit, 'body' | 'headers'> {
  body?: unknown
  headers?: Record<string, string>
  /** 写操作或 Commit 需传入至少 8 字符的幂等键 */
  idempotencyKey?: string
  /** 是否跳过自动附加租户请求头（如 /health、/api/v1/auth/login） */
  skipTenantHeaders?: boolean
}

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'

/** 解析基础 API 地址，优先读取 VITE_API_BASE_URL */
export function resolveApiBaseUrl(): string {
  const envUrl = import.meta.env?.VITE_API_BASE_URL as string | undefined
  return (envUrl && envUrl.trim().length > 0 ? envUrl : DEFAULT_API_BASE_URL).replace(/\/+$/, '')
}

/** 生成符合 OpenAPI minLength=8 约束的幂等键 */
export function generateIdempotencyKey(prefix = 'idem'): string {
  const rand =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
  return `${prefix}-${rand}`
}

/** 将非 2xx 响应解析为 ApiClientError，保证绝不吞掉 error_code 与 trace_id */
export async function parseErrorResponse(response: Response): Promise<ApiClientError> {
  let payload: Partial<ErrorResponse> | null = null
  try {
    payload = (await response.json()) as Partial<ErrorResponse>
  } catch {
    payload = null
  }

  const normalized: ErrorResponse = {
    error_code: payload?.error_code || `HTTP_${response.status}`,
    message: payload?.message || response.statusText || '请求失败',
    detail: payload?.detail ?? null,
    trace_id: payload?.trace_id || response.headers.get('X-Trace-Id') || 'unknown-trace',
  }
  return new ApiClientError(response.status, normalized)
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly context: ApiContextProvider
  private readonly fetcher: typeof fetch

  constructor(
    context: ApiContextProvider,
    baseUrl: string = resolveApiBaseUrl(),
    fetcher: typeof fetch = (...args) => globalThis.fetch(...args),
  ) {
    this.context = context
    this.baseUrl = baseUrl.replace(/\/+$/, '')
    this.fetcher = fetcher
  }

  getBaseUrl(): string {
    return this.baseUrl
  }

  /** 构建标准请求头：Authorization / X-Organization-Id / X-Project-Id / Idempotency-Key */
  buildHeaders(options: RequestOptions = {}, isFormData = false): Record<string, string> {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      ...(options.headers || {}),
    }

    if (!isFormData && options.body !== undefined && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json'
    }

    const token = this.context.getAccessToken()
    if (token) {
      headers['Authorization'] = `Bearer ${token}`
    }

    if (!options.skipTenantHeaders) {
      const orgId = this.context.getOrganizationId()
      const projectId = this.context.getProjectId()
      if (orgId) headers['X-Organization-Id'] = orgId
      if (projectId) headers['X-Project-Id'] = projectId
    }

    if (options.idempotencyKey) {
      headers['Idempotency-Key'] = options.idempotencyKey
    }

    return headers
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const url = `${this.baseUrl}${path.startsWith('/') ? path : `/${path}`}`
    const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData
    const headers = this.buildHeaders(options, isFormData)

    const init: RequestInit = {
      ...options,
      headers,
      body:
        options.body === undefined
          ? undefined
          : isFormData
            ? (options.body as FormData)
            : JSON.stringify(options.body),
    }

    const response = await this.fetcher(url, init)
    if (!response.ok) {
      throw await parseErrorResponse(response)
    }

    if (response.status === 204) {
      return undefined as T
    }

    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
      return (await response.json()) as T
    }
    return (await response.text()) as unknown as T
  }

  get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>(path, { ...options, method: 'GET' })
  }

  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>(path, { ...options, method: 'POST', body })
  }

  patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PATCH', body })
  }

  delete<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>(path, { ...options, method: 'DELETE' })
  }
}
