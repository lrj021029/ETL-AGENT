// 与冻结契约 (openapi.yaml / error_codes.yaml / realtime_events.yaml / execution_state_machine.yaml) 严格对齐的 TypeScript 类型定义

/** 标准分页元数据 */
export interface PageMeta {
  total: number
  page: number
  page_size: number
}

/** 统一 API 错误响应模型，匹配 contracts/openapi/error_codes.yaml */
export interface ErrorResponse {
  error_code: string
  message: string
  detail?: Record<string, unknown> | unknown[] | null
  trace_id: string
}

/** 所有视图通用的显式数据状态枚举 */
export type ViewDataState = 'loading' | 'empty' | 'error' | 'stale' | 'partial' | 'success'

/** 环境枚举 */
export type EnvironmentType = 'dev' | 'staging' | 'prod'

/** 系统固定角色与自定义角色 */
export type SystemRole =
  | 'MAKER'
  | 'DATA_CHECKER'
  | 'SECURITY_CHECKER'
  | 'OPERATOR'
  | 'AUDITOR'
  | 'ORG_ADMIN'
  | 'PROJECT_ADMIN'

/** 控制面健康检查响应 */
export interface HealthResponse {
  status: 'ok' | 'degraded'
  service: string
  version: string
  dependencies: Record<string, { status: string; detail?: string }>
}

// ==================== 1. 认证与组织项目 ====================

export interface LoginRequest {
  email: string
  password: string
}

export interface RegisterEmployeeRequest {
  email: string
  display_name: string
  password: string
  org_slug?: string
}

export interface AdminUserItem {
  id: string
  email: string
  display_name: string
  status: 'ACTIVE' | 'DISABLED'
  email_verified?: boolean
  org_role: 'MEMBER' | 'ORG_ADMIN' | string
  in_project: boolean
  project_roles: string[]
  created_at: string
}

export interface AdminCreateUserRequest {
  email: string
  display_name: string
  password: string
  org_role: 'MEMBER' | 'ORG_ADMIN'
  project_role_code: SystemRole
}

/** 密码登录成功后返回的邮件二次验证挑战 */
export interface OtpChallengeResponse {
  challenge_id: string
  masked_email: string
  expires_in_seconds: number
  delivery_channel: 'EMAIL_OTP'
  notice?: string
}

export interface OtpVerifyRequest {
  challenge_id: string
  otp_code: string
}

export interface TokenPairResponse {
  access_token: string
  refresh_token: string
  token_type: 'Bearer'
  expires_in: number
}

export interface OrganizationItem {
  id: string
  name: string
  slug: string
  role: string
}

export interface ProjectItem {
  id: string
  organization_id: string
  name: string
  slug: string
  environment: EnvironmentType
  roles: string[]
}

export interface UserProfile {
  id: string
  email: string
  display_name: string
  organizations: OrganizationItem[]
  projects: ProjectItem[]
  system_roles: string[]
}

// ==================== 2. 总览工作台 ====================

export interface ProjectOverviewResponse {
  organization_id: string
  project_id: string
  environment: EnvironmentType
  connections_total: number
  connections_healthy: number
  pipelines_total: number
  latest_versions_count: number
  pending_preparations: number
  pending_approvals: number
  pending_commits: number
  active_batch_runs: number
  active_cdc_runs: number
  success_rate: number | null
  reject_rate: number | null
  budget_utilization_pct: number | null
  recent_audit_events: EvidenceEventItem[]
}

// ==================== 3. 连接器注册表、数据源与元数据 ====================

export type ConnectorVerificationStatus =
  | 'END_TO_END_VERIFIED'
  | 'INTEGRATION_TESTED'
  | 'CONFIGURED'
  | 'PLANNED'

export interface ConnectorRegistryEntry {
  connector_type: string
  display_name: string
  modes: Array<'BATCH' | 'CDC'>
  directions: Array<'SOURCE' | 'SINK'>
  verification_status: ConnectorVerificationStatus
  supported_versions: string[]
  notes?: string
}

export interface ConnectionItem {
  id: string
  project_id: string
  name: string
  connector_type: string
  environment: EnvironmentType
  host_summary: string
  secret_ref: string
  secret_masked: string
  enabled: boolean
  last_test_status?: 'OK' | 'FAILED' | 'UNTESTED'
  last_tested_at?: string
  created_at: string
}

export interface CreateConnectionRequest {
  name: string
  connector_type: string
  environment: EnvironmentType
  config: Record<string, unknown>
  secret_ref: string
}

export interface ConnectionTestResult {
  connection_id: string
  reachable: boolean
  latency_ms?: number
  message: string
  checked_at: string
}

export interface ColumnMetadata {
  name: string
  data_type: string
  nullable: boolean
  is_primary_key: boolean
  sensitivity_tag?: 'NONE' | 'PII' | 'PCI' | 'CONFIDENTIAL' | 'RESTRICTED'
  masked_samples?: string[]
}

export interface TableMetadata {
  schema_name: string
  table_name: string
  approx_rows?: number
  columns: ColumnMetadata[]
}

export interface MetadataSnapshot {
  id: string
  connection_id: string
  schema_fingerprint: string
  captured_at: string
  tables: TableMetadata[]
  sample_retention_policy?: string
}

export interface SchemaDriftEvent {
  id: string
  from_snapshot_id: string
  to_snapshot_id: string
  severity: 'LOW' | 'MEDIUM' | 'HIGH' | 'BREAKING'
  diffs: Array<{
    table: string
    column: string
    change_type: 'ADDED' | 'REMOVED' | 'TYPE_CHANGED' | 'NULLABILITY_CHANGED'
    before?: string
    after?: string
  }>
  detected_at: string
}

// ==================== 4. Agent (LangGraph) 与 Pipeline Studio ====================

export interface AgentThreadState {
  thread_id: string
  project_id: string
  status: 'IDLE' | 'RUNNING' | 'INTERRUPTED' | 'COMPLETED' | 'FAILED'
  current_node?: string
  interrupt_questions?: Array<{
    field: string
    prompt: string
    required: boolean
    options?: string[]
  }>
  llm_redaction_status: {
    redacted: boolean
    policy_version: string
    external_provider?: string
  }
  draft_artifacts?: PipelineDraftArtifacts
  audit_summary?: Array<{ step: string; action: string; timestamp: string }>
}

export interface PipelineDraftArtifacts {
  etl_plan_dsl: Record<string, unknown>
  dag_nodes: Array<{
    id: string
    kind: 'SOURCE' | 'TRANSFORM' | 'SINK'
    label: string
    connector_type?: string
    execution_location?: string
  }>
  dag_edges: Array<{ id: string; source: string; target: string; label?: string }>
  sql_transformation: string
  sql_execution_location_reason?: string
  seatunnel_hocon: string
  schema_mapping_diff: Array<{
    source_column: string
    source_type: string
    target_column: string
    target_type: string
    sensitivity_before?: string
    masking_after?: string
  }>
  quality_contract: {
    rules: Array<{ rule_id: string; rule_type: string; column?: string; expression: string }>
    restricted_sql_enabled: boolean
  }
  runtime_supervision_contract: {
    max_read_rows: number
    max_write_bytes: number
    max_duration_seconds: number
    max_amplification_ratio: number
    reject_rate_threshold_pct: number
    rolling_window_size?: number
    min_sample_size?: number
    max_error_rows?: number
  }
}

export interface PipelineItem {
  id: string
  name: string
  description?: string
  source_connection_id: string
  sink_connection_id: string
  execution_mode: 'BATCH' | 'CDC'
  latest_version?: string
  latest_version_id?: string
  created_at: string
}

export interface GateValidationReport {
  passed: boolean
  repair_loop_count: number
  max_repair_loops: number
  checks: Array<{
    gate_name: 'SCHEMA' | 'SQL_AST' | 'HOCON' | 'CONNECTOR_CAPABILITY' | 'POLICY'
    passed: boolean
    errors: string[]
    warnings: string[]
    repair_suggestion?: string
  }>
}

export interface PipelineVersionItem {
  id: string
  pipeline_id: string
  version_number: number
  sha256_digest: string
  frozen_at: string
  frozen_by: string
  manifest: Record<string, unknown>
}

export interface ScheduleItem {
  id: string
  pipeline_id: string
  pipeline_version_id: string
  standing_preparation_id: string
  cron_expression: string
  misfire_policy: 'FORBID' | 'FIRE_ONCE'
  concurrency_policy: 'SKIP' | 'FORBID'
  enabled: boolean
  next_run_at?: string
}

// ==================== 5. Harness (Prepare / Approve / Commit) ====================

export type RiskLevel = 'P0' | 'P1' | 'P2' | 'P3'

export type PreparationStatus =
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'REJECTED'
  | 'COMMITTED'
  | 'INVALIDATED'
  | 'EXPIRED'

export interface ApprovalSlotItem {
  slot_type: 'DATA_CHECKER' | 'SECURITY_CHECKER'
  required: boolean
  status: 'PENDING' | 'APPROVED' | 'REJECTED'
  decided_by?: string
  decided_by_name?: string
  decided_at?: string
  reason?: string
  fact_summary?: string
}

export interface PreparationItem {
  id: string
  project_id: string
  pipeline_version_id: string
  preparation_type: 'EXECUTION' | 'STANDING_SCHEDULE' | 'ROLLBACK'
  risk_level: RiskLevel
  status: PreparationStatus
  input_fingerprint: string
  artifact_sha256: string
  metadata_snapshot_id: string
  resource_scope: string[]
  data_classification: string
  budget_summary: Record<string, number | string>
  environment: EnvironmentType
  secret_refs: string[]
  rollback_plan: string
  invalidation_reason?: string
  created_by: string
  created_at: string
  expires_at?: string
  approval_slots: ApprovalSlotItem[]
}

export interface CommitResponse {
  execution_id: string
  command_id: string
  confirmation_state: ConfirmationState
  status: string
}

// ==================== 6. ExecutionRun 与实时状态机 ====================

export type ConfirmationState =
  | 'REQUEST_SUBMITTED'
  | 'ENGINE_CONFIRMED'
  | 'RECONCILIATION_PENDING'

export type BatchExecutionStatus =
  | 'CREATED'
  | 'QUEUED'
  | 'SUBMITTING'
  | 'UNKNOWN'
  | 'RECONCILING'
  | 'RUNNING'
  | 'PAUSE_REQUESTED'
  | 'PAUSED'
  | 'CANCEL_REQUESTED'
  | 'VALIDATING'
  | 'PUBLISHING'
  | 'SUCCEEDED'
  | 'INTERRUPTED'
  | 'FAILED'
  | 'ROLLING_BACK'
  | 'ROLLED_BACK'
  | 'CANCELLED'

export type CdcExecutionStatus =
  | 'CREATED'
  | 'QUEUED'
  | 'SUBMITTING'
  | 'UNKNOWN'
  | 'RECONCILING'
  | 'INITIAL_SNAPSHOT'
  | 'STREAMING'
  | 'DEGRADED'
  | 'PAUSE_REQUESTED'
  | 'PAUSED'
  | 'CHECKPOINT_LOST'
  | 'POSITION_EXPIRED'
  | 'RECOVERING'
  | 'STOP_REQUESTED'
  | 'STOPPED'
  | 'FAILED'

export interface ExecutionMetricsSnapshot {
  sampled_at: string
  read_rows: number
  validated_rows: number
  valid_rows: number
  write_rows: number
  write_bytes: number
  error_rows: number
  reject_rate_total: number
  reject_rate_window: number
  throughput_rps: number
  duration_seconds: number
  amplification_ratio: number
  cdc_lag_ms?: number
}

export interface ExecutionRunItem {
  id: string
  project_id: string
  pipeline_version_id: string
  preparation_id: string
  execution_mode: 'BATCH' | 'CDC'
  status: BatchExecutionStatus | CdcExecutionStatus
  confirmation_state: ConfirmationState
  external_job_id?: string
  trace_id?: string
  checkpoint_position?: string
  restart_count?: number
  started_at: string
  finished_at?: string
  latest_metrics?: ExecutionMetricsSnapshot
  diagnosis?: {
    advisory_only: true
    summary: string
    root_cause_candidates: string[]
    suggested_actions: string[]
  }
}

export interface QualityReportResponse {
  execution_id: string
  passed: boolean
  validated_rows: number
  valid_rows: number
  error_rows: number
  reject_rate: number
  rule_results: Array<{
    rule_id: string
    rule_type: string
    passed: boolean
    failed_rows: number
  }>
  redacted_error_distribution: Array<{
    error_code: string
    count: number
    sample_redacted?: string
  }>
}

export interface PublishedDatasetVersionItem {
  id: string
  target_table: string
  execution_id: string
  pipeline_version_id: string
  row_count: number
  schema_fingerprint: string
  published_at: string
  is_active: boolean
}

export interface RollbackItem {
  id: string
  target_execution_id: string
  rollback_type: 'SHADOW_CLEANUP' | 'SWAP_RESTORE' | 'CDC_RECOVERY'
  status: string
  preparation_id: string
  created_at: string
}

// ==================== 7. 实时事件负载 ====================

export interface ExecutionStateChangedEvent {
  execution_id: string
  execution_mode: 'BATCH' | 'CDC'
  from_status: string
  to_status: string
  confirmation_state: ConfirmationState
  external_job_id?: string
  reason_code?: string
  occurred_at: string
  trace_id: string
}

export interface ExecutionSupervisionAlertEvent {
  execution_id: string
  breach_type: string
  threshold: number
  observed_value: number
  action_taken: string
  occurred_at: string
}

// ==================== 8. Evidence Ledger、PDP Policy 与 Benchmark ====================

export interface EvidenceEventItem {
  id: string
  seq: number
  event_type: string
  actor_id: string
  resource_type: string
  resource_id: string
  sha256_hash: string
  prev_hash: string
  trace_id: string
  occurred_at: string
}

export interface EvidenceCheckpointItem {
  id: string
  from_seq: number
  to_seq: number
  merkle_or_chain_root: string
  ed25519_signature: string
  key_id: string
  signed_at: string
}

export interface EvidenceVerifyResult {
  chain_valid: boolean
  checkpoint_signatures_valid: boolean
  verified_events_count: number
  tamper_detected_at_seq?: number | null
  message: string
}

export interface PolicyVersionItem {
  id: string
  version: string
  created_by: string
  created_at: string
  rules_json: Record<string, unknown>
  diff_from_prev?: string
}

export interface BenchmarkDatasetItem {
  id: string
  name: string
  scale: '10K' | '100K' | '1M'
  seed: number
  sha256: string
  created_at: string
}

export interface BenchmarkRunItem {
  id: string
  dataset_id: string
  level: 'L0' | 'L1' | 'L2'
  model_provider: string
  model_name: string
  prompt_version: string
  policy_version: string
  scoring_profile_version: string
  p0_block_rate: number
  p1_block_rate: number
  p0_p1_hard_gate_passed: boolean
  overall_verdict: 'PASSED' | 'FAILED_HARD_GATE' | 'FAILED_THRESHOLD'
  raw_metrics: {
    plan_validity: number
    hocon_validity: number
    schema_mapping_accuracy: number
    sql_safety_detection: number
    replay_prevention: number
    repair_success_rate: number
    end_to_end_success: number
  }
  composite_score: number
  executed_at: string
}
