// API 客户端单元测试：校验租户请求头、幂等键注入与 ErrorResponse 解析
import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiClientError, generateIdempotencyKey } from '@/api/client'

describe('ApiClient', () => {
  it('自动注入 Authorization、X-Organization-Id、X-Project-Id 与 Idempotency-Key', async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )

    const client = new ApiClient(
      {
        getAccessToken: () => 'jwt-token-123',
        getOrganizationId: () => 'org-uuid-1',
        getProjectId: () => 'proj-uuid-2',
      },
      'http://127.0.0.1:8000',
      mockFetch as unknown as typeof fetch,
    )

    const idemKey = generateIdempotencyKey('commit')
    expect(idemKey.length).toBeGreaterThanOrEqual(8)

    await client.post('/api/v1/projects/proj-uuid-2/preparations/prep-1/commit', {}, { idempotencyKey: idemKey })

    expect(mockFetch).toHaveBeenCalledTimes(1)
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://127.0.0.1:8000/api/v1/projects/proj-uuid-2/preparations/prep-1/commit')

    const headers = init.headers as Record<string, string>
    expect(headers['Authorization']).toBe('Bearer jwt-token-123')
    expect(headers['X-Organization-Id']).toBe('org-uuid-1')
    expect(headers['X-Project-Id']).toBe('proj-uuid-2')
    expect(headers['Idempotency-Key']).toBe(idemKey)
  })

  it('skipTenantHeaders 为 true 时不附加租户上下文头', async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )

    const client = new ApiClient(
      {
        getAccessToken: () => 'jwt-token-123',
        getOrganizationId: () => 'org-uuid-1',
        getProjectId: () => 'proj-uuid-2',
      },
      'http://127.0.0.1:8000',
      mockFetch as unknown as typeof fetch,
    )

    await client.get('/health', { skipTenantHeaders: true })
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers['X-Organization-Id']).toBeUndefined()
    expect(headers['X-Project-Id']).toBeUndefined()
  })

  it('完整解析后端 ErrorResponse（含 error_code、detail 与 trace_id）', async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: 'SOD_VIOLATION',
          message: 'Maker 不得审批自己的 Preparation',
          detail: { slot: 'DATA_CHECKER' },
          trace_id: 'trace-abc-999',
        }),
        {
          status: 403,
          headers: { 'content-type': 'application/json' },
        },
      ),
    )

    const client = new ApiClient(
      {
        getAccessToken: () => 'jwt',
        getOrganizationId: () => 'org-1',
        getProjectId: () => 'proj-1',
      },
      'http://127.0.0.1:8000',
      mockFetch as unknown as typeof fetch,
    )

    await expect(client.post('/api/v1/projects/proj-1/preparations/p1/approvals', {})).rejects.toSatisfy(
      (err: unknown) => {
        expect(err).toBeInstanceOf(ApiClientError)
        const apiErr = err as ApiClientError
        expect(apiErr.status).toBe(403)
        expect(apiErr.errorCode).toBe('SOD_VIOLATION')
        expect(apiErr.traceId).toBe('trace-abc-999')
        expect(apiErr.message).toContain('Maker')
        return true
      },
    )
  })
})
