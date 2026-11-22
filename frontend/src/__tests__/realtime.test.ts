// RealtimeStreamAdapter 单元测试：验证 SSE 主通道、confirmation_state 状态迁移与 HTTP 轮询兜底
import { describe, expect, it, vi } from 'vitest'
import { ApiClient } from '@/api/client'
import { RealtimeStreamAdapter } from '@/api/realtime'
import type { ConfirmationState, ExecutionStateChangedEvent } from '@/types/api'

class MockEventSource {
  onopen: ((ev: Event) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  private listeners = new Map<string, Array<(ev: MessageEvent) => void>>()
  closed = false

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  emit(type: string, data: unknown): void {
    const ev = new MessageEvent(type, { data: JSON.stringify(data) })
    for (const fn of this.listeners.get(type) ?? []) {
      fn(ev)
    }
  }

  triggerError(): void {
    this.onerror?.(new Event('error'))
  }

  close(): void {
    this.closed = true
  }
}

describe('RealtimeStreamAdapter', () => {
  it('优先使用 SSE 并正确更新 confirmation_state 与 metrics_tick', () => {
    const mockClient = new ApiClient(
      { getAccessToken: () => 't', getOrganizationId: () => 'o', getProjectId: () => 'p' },
      'http://127.0.0.1:8000',
    )
    let createdEs: MockEventSource | null = null

    const adapter = new RealtimeStreamAdapter({
      projectId: 'proj-1',
      executionId: 'exec-1',
      apiClient: mockClient,
      eventSourceFactory: () => {
        createdEs = new MockEventSource()
        return createdEs as unknown as EventSource
      },
    })

    const states: ConfirmationState[] = []
    const events: ExecutionStateChangedEvent[] = []
    adapter.start({
      onConfirmationStateChange: (cs) => states.push(cs),
      onStateChanged: (ev) => events.push(ev),
    })

    expect(adapter.transportMode).toBe('SSE')
    expect(createdEs).not.toBeNull()

    createdEs!.onopen?.(new Event('open'))
    createdEs!.emit('execution.state_changed', {
      execution_id: 'exec-1',
      execution_mode: 'BATCH',
      from_status: 'SUBMITTING',
      to_status: 'RUNNING',
      confirmation_state: 'ENGINE_CONFIRMED',
      external_job_id: 'zeta-job-42',
      occurred_at: '2026-11-22T10:00:00Z',
      trace_id: 'trace-1',
    })

    expect(adapter.confirmationState).toBe('ENGINE_CONFIRMED')
    expect(states).toContain('ENGINE_CONFIRMED')
    expect(events[0].external_job_id).toBe('zeta-job-42')
    adapter.stop()
    expect(createdEs!.closed).toBe(true)
  })

  it('SSE 失败时自动降级为 HTTP_POLLING 兜底并拉取状态', async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'exec-2',
          project_id: 'proj-1',
          pipeline_version_id: 'ver-1',
          preparation_id: 'prep-1',
          execution_mode: 'CDC',
          status: 'UNKNOWN',
          confirmation_state: 'RECONCILIATION_PENDING',
          started_at: '2026-11-22T10:00:00Z',
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    )

    const mockClient = new ApiClient(
      { getAccessToken: () => 't', getOrganizationId: () => 'o', getProjectId: () => 'p' },
      'http://127.0.0.1:8000',
      mockFetch as unknown as typeof fetch,
    )

    let createdEs: MockEventSource | null = null
    const adapter = new RealtimeStreamAdapter({
      projectId: 'proj-1',
      executionId: 'exec-2',
      apiClient: mockClient,
      pollingIntervalMs: 60000,
      eventSourceFactory: () => {
        createdEs = new MockEventSource()
        return createdEs as unknown as EventSource
      },
    })

    const modes: string[] = []
    adapter.start({
      onTransportChange: (mode) => modes.push(mode),
    })

    createdEs!.triggerError()
    await vi.waitFor(() => {
      expect(adapter.confirmationState).toBe('RECONCILIATION_PENDING')
    })

    expect(adapter.transportMode).toBe('HTTP_POLLING')
    expect(modes).toContain('HTTP_POLLING')
    adapter.stop()
  })
})
