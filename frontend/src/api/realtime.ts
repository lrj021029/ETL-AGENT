// 实时执行流适配器：主通道采用 SSE (EventSource)，异常或环境不支持时自动降级为 HTTP 轮询兜底
// 严格对齐 contracts/openapi/realtime_events.yaml
import type {
  ConfirmationState,
  ExecutionMetricsSnapshot,
  ExecutionRunItem,
  ExecutionStateChangedEvent,
  ExecutionSupervisionAlertEvent,
} from '@/types/api'
import type { ApiClient } from './client'

export type RealtimeTransportMode = 'SSE' | 'HTTP_POLLING'
export type RealtimeConnectionState = 'idle' | 'connecting' | 'connected' | 'stale' | 'disconnected'

export interface RealtimeAdapterCallbacks {
  onStateChanged?: (event: ExecutionStateChangedEvent) => void
  onMetricsTick?: (metrics: ExecutionMetricsSnapshot) => void
  onSupervisionAlert?: (alert: ExecutionSupervisionAlertEvent) => void
  onHeartbeat?: (serverTime: string) => void
  onConfirmationStateChange?: (state: ConfirmationState) => void
  onTransportChange?: (mode: RealtimeTransportMode, connection: RealtimeConnectionState) => void
  onError?: (error: Error) => void
}

export interface RealtimeAdapterOptions {
  projectId: string
  executionId: string
  apiClient: ApiClient
  /** 可注入自定义 EventSource 构造器以便单元测试 */
  eventSourceFactory?: (url: string) => EventSource
  /** HTTP 轮询间隔（毫秒），默认 3000ms */
  pollingIntervalMs?: number
  /** 心跳超时判定为 stale 的阈值（毫秒），默认 15000ms */
  staleThresholdMs?: number
}

export class RealtimeStreamAdapter {
  private readonly projectId: string
  private readonly executionId: string
  private readonly apiClient: ApiClient
  private readonly eventSourceFactory?: (url: string) => EventSource
  private readonly pollingIntervalMs: number
  private readonly staleThresholdMs: number

  private callbacks: RealtimeAdapterCallbacks = {}
  private eventSource: EventSource | null = null
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private staleTimer: ReturnType<typeof setTimeout> | null = null
  private stopped = false

  private _transportMode: RealtimeTransportMode = 'SSE'
  private _connectionState: RealtimeConnectionState = 'idle'
  private _confirmationState: ConfirmationState = 'REQUEST_SUBMITTED'
  private _lastEventAt: number | null = null

  constructor(options: RealtimeAdapterOptions) {
    this.projectId = options.projectId
    this.executionId = options.executionId
    this.apiClient = options.apiClient
    this.eventSourceFactory = options.eventSourceFactory
    this.pollingIntervalMs = options.pollingIntervalMs ?? 3000
    this.staleThresholdMs = options.staleThresholdMs ?? 15000
  }

  get transportMode(): RealtimeTransportMode {
    return this._transportMode
  }

  get connectionState(): RealtimeConnectionState {
    return this._connectionState
  }

  get confirmationState(): ConfirmationState {
    return this._confirmationState
  }

  get lastEventAt(): number | null {
    return this._lastEventAt
  }

  start(callbacks: RealtimeAdapterCallbacks): void {
    this.callbacks = callbacks
    this.stopped = false
    this.connectSse()
  }

  stop(): void {
    this.stopped = true
    if (this.eventSource) {
      this.eventSource.close()
      this.eventSource = null
    }
    if (this.pollTimer) {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    }
    if (this.staleTimer) {
      clearTimeout(this.staleTimer)
      this.staleTimer = null
    }
    this.updateTransport(this._transportMode, 'disconnected')
  }

  private updateTransport(mode: RealtimeTransportMode, state: RealtimeConnectionState): void {
    this._transportMode = mode
    this._connectionState = state
    this.callbacks.onTransportChange?.(mode, state)
  }

  private updateConfirmationState(next: ConfirmationState): void {
    if (this._confirmationState !== next) {
      this._confirmationState = next
      this.callbacks.onConfirmationStateChange?.(next)
    }
  }

  private markActivity(): void {
    this._lastEventAt = Date.now()
    if (this._connectionState === 'stale' || this._connectionState === 'connecting') {
      this.updateTransport(this._transportMode, 'connected')
    }
    if (this.staleTimer) {
      clearTimeout(this.staleTimer)
    }
    this.staleTimer = setTimeout(() => {
      if (!this.stopped) {
        this.updateTransport(this._transportMode, 'stale')
      }
    }, this.staleThresholdMs)
  }

  private buildSseUrl(): string {
    const base = this.apiClient.getBaseUrl()
    return `${base}/api/v1/projects/${encodeURIComponent(this.projectId)}/executions/${encodeURIComponent(this.executionId)}/events`
  }

  private connectSse(): void {
    const factory =
      this.eventSourceFactory ??
      (typeof EventSource !== 'undefined' ? (url: string) => new EventSource(url) : null)

    if (!factory) {
      // 运行环境无 EventSource 时直接切换为 HTTP 轮询兜底
      this.switchToPolling()
      return
    }

    this.updateTransport('SSE', 'connecting')
    try {
      const es = factory(this.buildSseUrl())
      this.eventSource = es

      es.onopen = () => {
        if (this.stopped) return
        this.updateTransport('SSE', 'connected')
        this.markActivity()
      }

      es.addEventListener('execution.state_changed', (ev: MessageEvent) => {
        if (this.stopped) return
        this.markActivity()
        try {
          const payload = JSON.parse(String(ev.data)) as ExecutionStateChangedEvent
          if (payload.confirmation_state) {
            this.updateConfirmationState(payload.confirmation_state)
          }
          this.callbacks.onStateChanged?.(payload)
        } catch (err) {
          this.callbacks.onError?.(err instanceof Error ? err : new Error(String(err)))
        }
      })

      es.addEventListener('execution.metrics_tick', (ev: MessageEvent) => {
        if (this.stopped) return
        this.markActivity()
        try {
          const payload = JSON.parse(String(ev.data)) as ExecutionMetricsSnapshot
          this.callbacks.onMetricsTick?.(payload)
        } catch (err) {
          this.callbacks.onError?.(err instanceof Error ? err : new Error(String(err)))
        }
      })

      es.addEventListener('execution.supervision_alert', (ev: MessageEvent) => {
        if (this.stopped) return
        this.markActivity()
        try {
          const payload = JSON.parse(String(ev.data)) as ExecutionSupervisionAlertEvent
          this.callbacks.onSupervisionAlert?.(payload)
        } catch (err) {
          this.callbacks.onError?.(err instanceof Error ? err : new Error(String(err)))
        }
      })

      es.addEventListener('execution.heartbeat', (ev: MessageEvent) => {
        if (this.stopped) return
        this.markActivity()
        try {
          const payload = JSON.parse(String(ev.data)) as { server_time: string }
          this.callbacks.onHeartbeat?.(payload.server_time)
        } catch {
          // 心跳解析失败不影响主流程
        }
      })

      es.onerror = () => {
        if (this.stopped) return
        es.close()
        this.eventSource = null
        // SSE 连接失败后自动降级为 HTTP 轮询兜底
        this.switchToPolling()
      }
    } catch {
      this.switchToPolling()
    }
  }

  private switchToPolling(): void {
    if (this.stopped) return
    if (this.pollTimer) {
      clearInterval(this.pollTimer)
    }
    this.updateTransport('HTTP_POLLING', 'connecting')
    void this.pollOnce()
    this.pollTimer = setInterval(() => {
      void this.pollOnce()
    }, this.pollingIntervalMs)
  }

  async pollOnce(): Promise<void> {
    if (this.stopped) return
    try {
      const run = await this.apiClient.get<ExecutionRunItem>(
        `/api/v1/projects/${encodeURIComponent(this.projectId)}/executions/${encodeURIComponent(this.executionId)}`,
      )
      if (this.stopped) return
      this.markActivity()
      this.updateTransport('HTTP_POLLING', 'connected')
      if (run.confirmation_state) {
        this.updateConfirmationState(run.confirmation_state)
      }
      this.callbacks.onStateChanged?.({
        execution_id: run.id,
        execution_mode: run.execution_mode,
        from_status: run.status,
        to_status: run.status,
        confirmation_state: run.confirmation_state,
        external_job_id: run.external_job_id,
        occurred_at: new Date().toISOString(),
        trace_id: run.trace_id || 'polling-fallback',
      })
      if (run.latest_metrics) {
        this.callbacks.onMetricsTick?.(run.latest_metrics)
      }
    } catch (err) {
      if (!this.stopped) {
        this.updateTransport('HTTP_POLLING', 'stale')
        this.callbacks.onError?.(err instanceof Error ? err : new Error(String(err)))
      }
    }
  }
}
