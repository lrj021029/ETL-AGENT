# Backend 控制面基础、多租户 IAM、PostgreSQL RLS、连接/元数据、受管调度与 Outbox 投递实现说明

> **负责模块**：Milestones M1 / M2 / M3 / M8 Backend Foundation, IAM, RLS, Connections, Metadata, Scheduling & Outbox Dispatcher  
> **对标契约**：`contracts/db/erd.md`、`contracts/db/rls_policy_spec.md`、`contracts/openapi/openapi.yaml`、`contracts/openapi/error_codes.yaml`、`contracts/openapi/realtime_events.yaml`、`docs/adr/ADR-002-rls-and-background-context.md`、`docs/adr/ADR-003-harness-capability-and-schedule.md`

---

## 1. 全领域 SQLAlchemy 2.0 ORM 与 Alembic 迁移（`backend/app/shared/models/` & `backend/migrations/`）

1. **六大领域 71 张业务表全覆盖**：
   - `iam.py`（13 表）：`users`, `organizations`, `organization_members`, `projects`, `project_members`, `roles`, `permissions`, `role_permissions`, `member_role_bindings`, `policy_rules`, `sessions`, `refresh_token_families`, `email_otp_challenges`
   - `connections_metadata.py`（13 表）：`secret_references`, `data_connections`, `connection_versions`, `metadata_profiles`, `metadata_snapshots`, `metadata_objects`, `metadata_columns`, `metadata_statistics`, `sensitive_classifications`, `schema_fingerprints`, `schema_drift_events`, `uploaded_files`, `object_storage_refs`
   - `pipeline_artifacts.py`（13 表）：`agent_threads`, `workflow_runs`, `pipeline_definitions`, `pipeline_drafts`, `pipeline_versions`, `artifact_manifests`, `artifact_blobs`, `etl_plan_modules`, `sql_artifacts`, `hocon_artifacts`, `quality_contracts`, `runtime_contracts`, `connector_capability_versions`
   - `governance_harness.py`（10 表）：`preparations`, `preparation_fingerprints`, `approval_slots`, `approval_decisions`, `capability_issuances`, `capability_consumptions`, `execution_commands`, `replay_records`, `outbox_events`, `outbox_deliveries`
   - `execution_quality.py`（12 表）：`execution_runs`, `execution_state_events`, `external_job_refs`, `execution_metrics`, `execution_logs_refs`, `quality_reports`, `quality_rule_results`, `rejected_record_summaries`, `published_dataset_versions`, `rollback_runs`, `schedules`, `schedule_triggers`
   - `evidence_benchmark.py`（10 表）：`evidence_events`, `evidence_checkpoints`, `evidence_verification_runs`, `benchmark_datasets`, `benchmark_cases`, `benchmark_runs`, `benchmark_case_results`, `model_provider_versions`, `policy_versions`, `generator_versions`
2. **Alembic 迁移拓扑**：
   - `0001_initial_schema.py`：创建全部 71 张表、复合租户索引 `(organization_id, project_id)` 与关键唯一约束（`capability_consumptions.jti`、`execution_commands.command_id`、`evidence_events(organization_id, project_id, sequence_no)`）。
   - `0002_rls_policies.py`：配置 `etl_migrator`、`etl_runtime` (`NOBYPASSRLS`)、`etl_outbox_dispatcher` 三角色物理隔离，对所有组织级与项目级表开启 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`，并创建 `resolve_execution_tenant_context(UUID, UUID)` `SECURITY DEFINER` 函数。

## 2. 认证、邮件二次验证（Email OTP）与多租户 IAM（`backend/app/auth/` & `backend/app/memberships/`）

- **邮件二次验证（Email OTP）**：`/api/v1/auth/login` 校验密码后生成 Email OTP 挑战并投递至 Mailpit SMTP（响应明确标注 `verification_label: "邮件二次验证"`），经 `/api/v1/auth/otp/verify` 核验后签发 JWT Access Token 与绑定 Family 的 Refresh Token。
- **Refresh Token 家族轮转与重放检测**：每次 `/api/v1/auth/refresh` 递增 `rotation_count` 并更新 `current_token_hash`；一旦检测到旧 Refresh Token 被重放（Reuse Detection），立即吊销整个 `RefreshTokenFamily` 与关联 `UserSession`，返回 `AUTH_REFRESH_REUSE_DETECTED` (401)。
- **Org + Project 双重成员校验**：所有项目级 API 强制同时校验 `OrganizationMember` 与 `ProjectMember` 均处于 `ACTIVE`；一旦移除用户的组织成员资格，立即拒绝其访问该组织下的所有 Project（`TENANT_ACCESS_DENIED` 403）。
- **固定系统角色不可变与 SoD 硬约束**：`MAKER`、`DATA_CHECKER`、`SECURITY_CHECKER`、`OPERATOR`、`AUDITOR`、`ORG_ADMIN`、`PROJECT_ADMIN` 严禁删改或被自定义角色覆盖（`SYSTEM_ROLE_IMMUTABLE` 409）；自定义角色企图绕过职责分离或组合自批权限时硬拦截（`SOD_VIOLATION` 403）。

## 3. 连接器、路径逃逸防护与不可变元数据快照（`backend/app/connections/` & `backend/app/metadata/`）

- **零明文凭据**：`sanitize_non_secret_config` 自动剥离密码与内嵌密码 URI 并交由 `SecretProviderProtocol` 加密托管，数据库仅存 `secret_ref_id` 与 `secret_version`。
- **路径逃逸守卫（`validate_safe_file_path`）**：严格阻断 `..`、URL 编码逃逸、空字节、宿主机绝对路径（POSIX / Windows 盘符 / UNC）以及指向受控目录外的符号链接（Symlink Escape），抛出 `FILE_PATH_TRAVERSAL_BLOCKED` (403)。
- **不可变 `MetadataSnapshot` 与 `SchemaDriftEvent`**：每次探查刷新始终插入新 `MetadataSnapshot`，自动识别敏感列并对样本行执行脱敏，同时对比上一快照生成 `SchemaDriftEvent`。

## 4. 受管 Schedule 与 Outbox 租约投递器（`backend/app/scheduling/` & `backend/workers/`）

- **Schedule 强制绑定已审批 `StandingSchedulePreparation`**：校验 5 段 Cron 表达式与 IANA 时区/DST，强制执行 `FORBID` / `REPLACE` / `ALLOW` 并发策略与 `SKIP` / `FIRE_ONCE` / `CATCH_UP` 错过策略。
- **Outbox Dispatcher**：使用 `etl_outbox_dispatcher` 角色执行 `SELECT ... FOR UPDATE SKIP LOCKED` 租约领取并投递 Celery，校验 `OutboxEventPayload` 契约，并在超过 `max_attempts` 后转入 `FAILED`。
