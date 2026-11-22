// 控制面 16 模块强类型 API 调用集合，严格对应 contracts/openapi/openapi.yaml
import type {
  AgentThreadState,
  ApprovalSlotItem,
  BenchmarkDatasetItem,
  BenchmarkRunItem,
  CommitResponse,
  ConnectionItem,
  ConnectionTestResult,
  ConnectorRegistryEntry,
  CreateConnectionRequest,
  EvidenceCheckpointItem,
  EvidenceEventItem,
  EvidenceVerifyResult,
  ExecutionRunItem,
  GateValidationReport,
  HealthResponse,
  LoginRequest,
  MetadataSnapshot,
  OrganizationItem,
  OtpChallengeResponse,
  OtpVerifyRequest,
  PipelineDraftArtifacts,
  PipelineItem,
  PipelineVersionItem,
  PolicyVersionItem,
  PreparationItem,
  ProjectItem,
  ProjectOverviewResponse,
  PublishedDatasetVersionItem,
  QualityReportResponse,
  RollbackItem,
  ScheduleItem,
  SchemaDriftEvent,
  TokenPairResponse,
  UserProfile,
} from '@/types/api'
import { ApiClient, generateIdempotencyKey } from './client'

/** 创建绑定了租户上下文的 API 服务方法集 */
export function createApiEndpoints(client: ApiClient) {
  return {
    // 0. Health
    getHealth: () => client.get<HealthResponse>('/health', { skipTenantHeaders: true }),

    // 1. Auth
    login: (body: LoginRequest) =>
      client.post<OtpChallengeResponse>('/api/v1/auth/login', body, { skipTenantHeaders: true }),
    verifyOtp: (body: OtpVerifyRequest) =>
      client.post<TokenPairResponse>('/api/v1/auth/otp/verify', body, { skipTenantHeaders: true }),
    refreshToken: (refreshToken: string) =>
      client.post<TokenPairResponse>(
        '/api/v1/auth/refresh',
        { refresh_token: refreshToken },
        { skipTenantHeaders: true },
      ),
    logout: () => client.post<void>('/api/v1/auth/logout', {}, { skipTenantHeaders: true }),
    getMe: () => client.get<UserProfile>('/api/v1/auth/me', { skipTenantHeaders: true }),

    // 2. Organizations & 3. Projects
    listOrganizations: () => client.get<{ items: OrganizationItem[] }>('/api/v1/organizations'),
    listProjects: () => client.get<{ items: ProjectItem[] }>('/api/v1/projects'),
    getOverview: (projectId: string) =>
      client.get<ProjectOverviewResponse>(`/api/v1/projects/${projectId}/overview`),

    // 4. Connections & Connector Registry
    getConnectorRegistry: () =>
      client.get<{ items: ConnectorRegistryEntry[] }>('/api/v1/connectors/registry', {
        skipTenantHeaders: true,
      }),
    listConnections: (projectId: string) =>
      client.get<{ items: ConnectionItem[] }>(`/api/v1/projects/${projectId}/connections`),
    createConnection: (projectId: string, body: CreateConnectionRequest) =>
      client.post<ConnectionItem>(`/api/v1/projects/${projectId}/connections`, body, {
        idempotencyKey: generateIdempotencyKey('conn'),
      }),
    testConnection: (projectId: string, connectionId: string) =>
      client.post<ConnectionTestResult>(
        `/api/v1/projects/${projectId}/connections/${connectionId}/test`,
        {},
      ),
    uploadFile: (projectId: string, file: File) => {
      const formData = new FormData()
      formData.append('file', file)
      return client.post<{ object_key: string; bucket: string; size_bytes: number }>(
        `/api/v1/projects/${projectId}/files/upload`,
        formData,
        { idempotencyKey: generateIdempotencyKey('upload') },
      )
    },

    // 5. Metadata
    probeMetadata: (projectId: string, connectionId: string) =>
      client.post<MetadataSnapshot>(
        `/api/v1/projects/${projectId}/connections/${connectionId}/metadata/probe`,
        {},
      ),
    listMetadataSnapshots: (projectId: string, connectionId: string) =>
      client.get<{ items: MetadataSnapshot[] }>(
        `/api/v1/projects/${projectId}/connections/${connectionId}/metadata/snapshots`,
      ),
    getSchemaDrift: (projectId: string, connectionId: string) =>
      client.get<{ items: SchemaDriftEvent[] }>(
        `/api/v1/projects/${projectId}/connections/${connectionId}/metadata/drift`,
      ),
    updateColumnClassification: (
      projectId: string,
      snapshotId: string,
      body: { table_name: string; column_name: string; sensitivity_tag: string },
    ) =>
      client.patch<MetadataSnapshot>(
        `/api/v1/projects/${projectId}/metadata/snapshots/${snapshotId}/classifications`,
        body,
      ),

    // 6. Agent (LangGraph)
    createAgentThread: (
      projectId: string,
      body: { source_connection_id: string; sink_connection_id: string },
    ) => client.post<AgentThreadState>(`/api/v1/projects/${projectId}/agent/threads`, body),
    invokeAgentThread: (
      projectId: string,
      threadId: string,
      body: { requirement: string; execution_mode: 'BATCH' | 'CDC' },
    ) =>
      client.post<AgentThreadState>(
        `/api/v1/projects/${projectId}/agent/threads/${threadId}/invoke`,
        body,
      ),
    resumeAgentThread: (
      projectId: string,
      threadId: string,
      body: { answers: Record<string, string> },
    ) =>
      client.post<AgentThreadState>(
        `/api/v1/projects/${projectId}/agent/threads/${threadId}/resume`,
        body,
      ),
    getAgentThread: (projectId: string, threadId: string) =>
      client.get<AgentThreadState>(`/api/v1/projects/${projectId}/agent/threads/${threadId}`),

    // 7. Pipeline & Artifacts & 12. Scheduling
    listPipelines: (projectId: string) =>
      client.get<{ items: PipelineItem[] }>(`/api/v1/projects/${projectId}/pipelines`),
    createPipeline: (
      projectId: string,
      body: {
        name: string
        description?: string
        source_connection_id: string
        sink_connection_id: string
        execution_mode: 'BATCH' | 'CDC'
      },
    ) => client.post<PipelineItem>(`/api/v1/projects/${projectId}/pipelines`, body),
    validateDraft: (projectId: string, pipelineId: string, draft: PipelineDraftArtifacts) =>
      client.post<GateValidationReport>(
        `/api/v1/projects/${projectId}/pipelines/${pipelineId}/drafts/validate`,
        draft,
      ),
    listPipelineVersions: (projectId: string, pipelineId: string) =>
      client.get<{ items: PipelineVersionItem[] }>(
        `/api/v1/projects/${projectId}/pipelines/${pipelineId}/versions`,
      ),
    freezePipelineVersion: (
      projectId: string,
      pipelineId: string,
      body: { draft: PipelineDraftArtifacts; comment?: string },
    ) =>
      client.post<PipelineVersionItem>(
        `/api/v1/projects/${projectId}/pipelines/${pipelineId}/versions`,
        body,
        { idempotencyKey: generateIdempotencyKey('freeze') },
      ),
    getVersionManifest: (projectId: string, versionId: string) =>
      client.get<{ sha256_digest: string; manifest: Record<string, unknown> }>(
        `/api/v1/projects/${projectId}/pipeline-versions/${versionId}/manifest`,
      ),
    listSchedules: (projectId: string) =>
      client.get<{ items: ScheduleItem[] }>(`/api/v1/projects/${projectId}/schedules`),
    createSchedule: (
      projectId: string,
      body: {
        pipeline_id: string
        pipeline_version_id: string
        standing_preparation_id: string
        cron_expression: string
        misfire_policy: 'FORBID' | 'FIRE_ONCE'
        concurrency_policy: 'SKIP' | 'FORBID'
      },
    ) => client.post<ScheduleItem>(`/api/v1/projects/${projectId}/schedules`, body),

    // 8. Preparation, 9. Approvals, 10. Commit
    listPreparations: (projectId: string) =>
      client.get<{ items: PreparationItem[] }>(`/api/v1/projects/${projectId}/preparations`),
    createPreparation: (
      projectId: string,
      body: {
        pipeline_version_id: string
        preparation_type: 'EXECUTION' | 'STANDING_SCHEDULE' | 'ROLLBACK'
      },
    ) =>
      client.post<PreparationItem>(`/api/v1/projects/${projectId}/preparations`, body, {
        idempotencyKey: generateIdempotencyKey('prep'),
      }),
    getPreparation: (projectId: string, preparationId: string) =>
      client.get<PreparationItem>(`/api/v1/projects/${projectId}/preparations/${preparationId}`),
    getApprovals: (projectId: string, preparationId: string) =>
      client.get<{ slots: ApprovalSlotItem[] }>(
        `/api/v1/projects/${projectId}/preparations/${preparationId}/approvals`,
      ),
    submitApproval: (
      projectId: string,
      preparationId: string,
      body: {
        slot_type: 'DATA_CHECKER' | 'SECURITY_CHECKER'
        decision: 'APPROVED' | 'REJECTED'
        reason: string
      },
    ) =>
      client.post<ApprovalSlotItem>(
        `/api/v1/projects/${projectId}/preparations/${preparationId}/approvals`,
        body,
      ),
    commitPreparation: (
      projectId: string,
      preparationId: string,
      idempotencyKey: string = generateIdempotencyKey('commit'),
    ) =>
      client.post<CommitResponse>(
        `/api/v1/projects/${projectId}/preparations/${preparationId}/commit`,
        {},
        { idempotencyKey },
      ),

    // 11. Execution & 13. Quality & 14. Rollback
    listExecutions: (projectId: string, mode?: 'BATCH' | 'CDC') =>
      client.get<{ items: ExecutionRunItem[] }>(
        `/api/v1/projects/${projectId}/executions${mode ? `?mode=${mode}` : ''}`,
      ),
    getExecution: (projectId: string, executionId: string) =>
      client.get<ExecutionRunItem>(`/api/v1/projects/${projectId}/executions/${executionId}`),
    cancelExecution: (projectId: string, executionId: string) =>
      client.post<{ status: string }>(
        `/api/v1/projects/${projectId}/executions/${executionId}/cancel`,
        {},
        { idempotencyKey: generateIdempotencyKey('cancel') },
      ),
    reconcileExecution: (projectId: string, executionId: string) =>
      client.post<{ status: string }>(
        `/api/v1/projects/${projectId}/executions/${executionId}/reconcile`,
        {},
      ),
    getQualityReport: (projectId: string, executionId: string) =>
      client.get<QualityReportResponse>(
        `/api/v1/projects/${projectId}/executions/${executionId}/quality-report`,
      ),
    listPublishedDatasets: (projectId: string) =>
      client.get<{ items: PublishedDatasetVersionItem[] }>(
        `/api/v1/projects/${projectId}/published-datasets`,
      ),
    prepareRollback: (
      projectId: string,
      body: { execution_id: string; rollback_type: 'SHADOW_CLEANUP' | 'SWAP_RESTORE' | 'CDC_RECOVERY' },
    ) =>
      client.post<PreparationItem>(`/api/v1/projects/${projectId}/rollbacks/prepare`, body, {
        idempotencyKey: generateIdempotencyKey('rollback'),
      }),
    listRollbacks: (projectId: string) =>
      client.get<{ items: RollbackItem[] }>(`/api/v1/projects/${projectId}/rollbacks`),

    // 15. Evidence Ledger & 16. Benchmark / Policies
    listEvidenceEvents: (projectId: string) =>
      client.get<{ items: EvidenceEventItem[] }>(`/api/v1/projects/${projectId}/evidence/events`),
    listEvidenceCheckpoints: (projectId: string) =>
      client.get<{ items: EvidenceCheckpointItem[] }>(
        `/api/v1/projects/${projectId}/evidence/checkpoints`,
      ),
    verifyEvidenceChain: (projectId: string) =>
      client.post<EvidenceVerifyResult>(`/api/v1/projects/${projectId}/evidence/verify`, {}),
    listBenchmarkDatasets: (projectId: string) =>
      client.get<{ items: BenchmarkDatasetItem[] }>(
        `/api/v1/projects/${projectId}/benchmarks/datasets`,
      ),
    generateBenchmarkDataset: (
      projectId: string,
      body: { name: string; scale: '10K' | '100K' | '1M'; seed: number },
    ) =>
      client.post<BenchmarkDatasetItem>(
        `/api/v1/projects/${projectId}/benchmarks/datasets`,
        body,
      ),
    listBenchmarkRuns: (projectId: string) =>
      client.get<{ items: BenchmarkRunItem[] }>(`/api/v1/projects/${projectId}/benchmarks/runs`),
    triggerBenchmarkRun: (
      projectId: string,
      body: { dataset_id: string; level: 'L0' | 'L1' | 'L2'; policy_version: string },
    ) =>
      client.post<BenchmarkRunItem>(`/api/v1/projects/${projectId}/benchmarks/runs`, body, {
        idempotencyKey: generateIdempotencyKey('bench'),
      }),
    listPolicies: (projectId: string) =>
      client.get<{ items: PolicyVersionItem[] }>(`/api/v1/projects/${projectId}/policies`),
    createPolicy: (
      projectId: string,
      body: { version: string; rules_json: Record<string, unknown> },
    ) => client.post<PolicyVersionItem>(`/api/v1/projects/${projectId}/policies`, body),
  }
}
