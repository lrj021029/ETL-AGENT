# Layer 0 契约：核心领域数据模型（ERD & Schema Ownership）

- **Contract Version**：`1.0.0`
- **Owner**：Lead / Architect（契约冻结）；Backend / IAM（`backend/migrations/` 唯一 Alembic 迁移 Owner）
- **Consumers**：Backend, Agent/DSL, Security/Harness, Data/SeaTunnel, Observability/Benchmark, QA

## 1. 全局表规范

1. 主键统一使用 `UUID`（`gen_random_uuid()` 或应用侧 UUIDv7/UUIDv4）。
2. 所有时间戳统一 `TIMESTAMPTZ`（UTC 存储）。
3. 所有租户业务表必须包含 `organization_id UUID NOT NULL`（项目级资源同时包含 `project_id UUID NOT NULL`），并建立 `(organization_id, project_id)` 复合索引。
4. 禁止在任何业务表存储数据库密码、LLM API Key、OAuth Token、私钥明文；仅存 `secret_ref_id` 与 `secret_version`。
5. 所有受 RLS 保护表均归属 `etl_migrator`，并启用 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`。

## 2. 六大领域实体定义

### 2.1 IAM / Tenant（13 表）
- `users(id, email UNIQUE, display_name, password_hash, status, email_verified, created_at, updated_at)` — 全局身份表（无 org RLS，按认证上下文访问）
- `organizations(id, slug UNIQUE, name, status, created_at, updated_at)`
- `organization_members(id, organization_id, user_id, org_role, status, joined_at, UNIQUE(organization_id, user_id))`
- `projects(id, organization_id, slug, name, environment, llm_data_policy_json, status, created_at, UNIQUE(organization_id, slug))`
- `project_members(id, organization_id, project_id, user_id, status, joined_at, UNIQUE(project_id, user_id))`
- `roles(id, organization_id NULLABLE, project_id NULLABLE, code, name, is_system BOOLEAN, description, created_at)` — `is_system=true` 不可删改
- `permissions(id, code UNIQUE, resource_type, action, description)`
- `role_permissions(role_id, permission_id, PRIMARY KEY(role_id, permission_id))`
- `member_role_bindings(id, organization_id, project_id NULLABLE, user_id, role_id, granted_by, granted_at)`
- `policy_rules(id, organization_id, project_id NULLABLE, policy_version, name, risk_level, rule_json, enabled, created_by, created_at)`
- `sessions(id, user_id, organization_id NULLABLE, ip_hash, user_agent, otp_verified, expires_at, revoked_at, created_at)`
- `refresh_token_families(id, user_id, session_id, current_token_hash, rotation_count, revoked_at, expires_at, created_at)`
- `email_otp_challenges(id, user_id, session_id, email, code_hash, attempts, max_attempts, expires_at, consumed_at, created_at)`

### 2.2 Connections / Metadata / Secrets（13 表）
- `secret_references(id, organization_id, project_id, provider_type, secret_path, current_version, masked_preview, status, created_by, created_at, updated_at)`
- `data_connections(id, organization_id, project_id, name, connector_id, connection_type, config_non_secret_json, secret_ref_id, current_version, status, last_health_status, last_checked_at, created_by, created_at)`
- `connection_versions(id, organization_id, project_id, connection_id, version_no, config_non_secret_json, secret_ref_id, secret_version, config_hash, created_by, created_at, UNIQUE(connection_id, version_no))`
- `metadata_profiles(id, organization_id, project_id, connection_id, refresh_cron NULLABLE, sample_policy_json, created_at)`
- `metadata_snapshots(id, organization_id, project_id, profile_id, connection_id, connection_version, connector_version, schema_fingerprint, classification_rule_version, stats_source, captured_at, captured_by)` — **不可原地更新**
- `metadata_objects(id, organization_id, project_id, snapshot_id, schema_name, object_name, object_type, row_count_approx, bytes_approx, primary_key_json, indexes_json)`
- `metadata_columns(id, organization_id, project_id, object_id, column_name, data_type, nullable, ordinal_position, default_expr, comment)`
- `metadata_statistics(id, organization_id, project_id, snapshot_id, object_id, stats_json, masked_sample_json, sample_expires_at)`
- `sensitive_classifications(id, organization_id, project_id, snapshot_id, object_id, column_name, classification_level, pii_category, source_mode, marked_by, marked_at)`
- `schema_fingerprints(id, organization_id, project_id, connection_id, snapshot_id, fingerprint_sha256, computed_at)`
- `schema_drift_events(id, organization_id, project_id, connection_id, from_snapshot_id, to_snapshot_id, severity, diff_json, detected_at)`
- `uploaded_files(id, organization_id, project_id, filename, media_type, size_bytes, sha256, object_storage_ref_id, uploaded_by, expires_at, created_at)`
- `object_storage_refs(id, organization_id, project_id, bucket, object_key, etag, sha256, size_bytes, created_at)`

### 2.3 Pipeline / Agent / Artifacts（13 表 + LangGraph PostgresSaver 表）
- `agent_threads(id, organization_id, project_id, user_id, langgraph_thread_id UNIQUE, title, status, pending_clarification_json, created_at, updated_at)`
- `workflow_runs(id, organization_id, project_id, thread_id, pipeline_draft_id NULLABLE, status, repair_attempts, max_repair_attempts, model_provider_version_id, llm_audit_summary_json, started_at, ended_at)`
- `pipeline_definitions(id, organization_id, project_id, name, description, sync_mode, source_connection_id, sink_connection_id, latest_revision, created_by, created_at)`
- `pipeline_drafts(id, organization_id, project_id, pipeline_id, thread_id NULLABLE, etl_plan_json, sql_text, hocon_text, quality_contract_json, runtime_contract_json, gate_report_json, updated_by, updated_at)`
- `pipeline_versions(id, organization_id, project_id, pipeline_id, revision, parent_revision NULLABLE, artifact_hash, manifest_id, status, created_by, created_at, UNIQUE(pipeline_id, revision))` — **不可变**
- `artifact_manifests(id, organization_id, project_id, pipeline_version_id, schema_version, canonical_json, artifact_hash, generator_metadata_json, compiler_version, created_at)` — **不可变**
- `artifact_blobs(id, organization_id, project_id, sha256, content_type, storage_ref_id, size_bytes, created_at)`
- `etl_plan_modules(id, organization_id, project_id, pipeline_version_id, module_type, dsl_json, content_hash)`
- `sql_artifacts(id, organization_id, project_id, pipeline_version_id, sql_purpose, dialect, sql_text, execution_strategy, strategy_reason_codes, ast_report_json, content_hash)`
- `hocon_artifacts(id, organization_id, project_id, pipeline_version_id, hocon_template_text, secret_placeholders_json, validation_report_json, content_hash)`
- `quality_contracts(id, organization_id, project_id, pipeline_version_id NULLABLE, contract_json, content_hash, created_by, created_at)`
- `runtime_contracts(id, organization_id, project_id, pipeline_version_id NULLABLE, contract_json, content_hash, created_by, created_at)`
- `connector_capability_versions(id, connector_id, connector_name, connector_version, seatunnel_version, capabilities_json, support_status, test_evidence_ref, verified_at, UNIQUE(connector_id, connector_version, seatunnel_version))`

### 2.4 Governance / Harness（10 表）
- `preparations(id, organization_id, project_id, preparation_type, pipeline_id, pipeline_version_id, artifact_hash, environment, risk_level, policy_version, status, frozen_facts_json, rollback_plan_json, budget_json, fingerprint_sha256, created_by, expires_at, created_at)` — `preparation_type IN ('EXECUTION','ROLLBACK','STANDING_SCHEDULE')`
- `preparation_fingerprints(id, organization_id, project_id, preparation_id, components_json, fingerprint_sha256, computed_at)`
- `approval_slots(id, organization_id, project_id, preparation_id, slot_role, required, status, UNIQUE(preparation_id, slot_role))` — `slot_role IN ('DATA_CHECKER','SECURITY_CHECKER','SINGLE_CHECKER','AUTO_P3')`
- `approval_decisions(id, organization_id, project_id, preparation_id, slot_id, approver_id, decision, reason, fact_digest_sha256, decided_at)`
- `capability_issuances(id, organization_id, project_id, jti UNIQUE, preparation_id, pipeline_version_hash, approval_digest, policy_version, tool, subject_id, environment, signing_key_id, nonce, issued_at, expires_at)`
- `capability_consumptions(id, organization_id, project_id, jti UNIQUE, consumed_by, execution_id, command_id, consumed_at)` — **`jti` 唯一约束为单次消费终极事实**
- `execution_commands(id, organization_id, project_id, command_id UNIQUE, idempotency_key, execution_id, command_type, capability_jti, payload_json, status, created_at)`
- `replay_records(id, organization_id, project_id, jti, nonce, detected_at, source_component, reason)`
- `outbox_events(id, organization_id, project_id, aggregate_type, aggregate_id, event_type, payload_ref_json, created_at)`
- `outbox_deliveries(id, outbox_event_id UNIQUE, status, attempts, max_attempts, lease_owner, lease_until, delivered_at, last_error, updated_at)`

### 2.5 Execution / Quality / Publish（12 表）
- `execution_runs(id, organization_id, project_id, pipeline_id, pipeline_version_id, artifact_hash, execution_mode, preparation_id, capability_jti, command_id UNIQUE, external_engine, external_job_id, status, status_reason, source_position_json, checkpoint_ref, metrics_snapshot_json, published_dataset_version_id NULLABLE, diagnosis_json NULLABLE, started_at, ended_at, created_at)`
- `execution_state_events(id, organization_id, project_id, execution_id, from_status, to_status, reason_code, detail_json, actor_id, occurred_at)`
- `external_job_refs(id, organization_id, project_id, execution_id, engine_type, stable_submit_id UNIQUE, external_job_id NULLABLE, submit_state, last_reconciled_at, raw_status_json)`
- `execution_metrics(id, organization_id, project_id, execution_id, sampled_at, read_rows, read_bytes, validated_rows, valid_rows, write_rows, write_bytes, error_rows, reject_rate_total, reject_rate_window, throughput_rps, duration_seconds, amplification_ratio, cdc_lag_ms, checkpoint_position)`
- `execution_logs_refs(id, organization_id, project_id, execution_id, trace_id, loki_query_ref, error_summary, created_at)`
- `quality_reports(id, organization_id, project_id, execution_id UNIQUE, contract_id, validated_rows, valid_rows, error_rows, reject_rate, passed, failure_reasons_json, created_at)`
- `quality_rule_results(id, organization_id, project_id, quality_report_id, rule_id, rule_type, checked_rows, failed_rows, passed, detail_json)`
- `rejected_record_summaries(id, organization_id, project_id, execution_id, error_table_ref, rule_id, error_code, count_rows, sample_redacted_json, updated_at)`
- `published_dataset_versions(id, organization_id, project_id, execution_id, pipeline_version_id, artifact_hash, target_connection_id, target_table_ref, shadow_table_ref, previous_version_id NULLABLE, archive_table_ref NULLABLE, swap_result_json, quality_report_id, status, published_by, published_at, retention_until NULLABLE)`
- `rollback_runs(id, organization_id, project_id, rollback_type, target_execution_id, target_published_version_id NULLABLE, preparation_id, capability_jti, command_id UNIQUE, status, result_json, initiated_by, started_at, ended_at)`
- `schedules(id, organization_id, project_id, pipeline_id, pipeline_version_id, standing_preparation_id, cron_expression, timezone, enabled, concurrency_policy, misfire_policy, version_binding_mode, next_run_at, last_triggered_at, created_by, created_at)`
- `schedule_triggers(id, organization_id, project_id, schedule_id, scheduled_for, triggered_at, status, execution_id NULLABLE, skip_reason NULLABLE)`

### 2.6 Evidence / Benchmark（10 表）
- `evidence_events(id, organization_id, project_id, sequence_no, timestamp, actor, event_type, resource_ref, artifact_hash, payload_digest, previous_hash, event_hash, UNIQUE(organization_id, project_id, sequence_no))` — **只追加**
- `evidence_checkpoints(id, organization_id, project_id, from_sequence, to_sequence, head_event_hash, signing_key_id, signature_b64url, signed_at)`
- `evidence_verification_runs(id, organization_id, project_id, verified_up_to_sequence, chain_valid, signatures_valid, broken_event_id NULLABLE, verified_at, verified_by)`
- `benchmark_datasets(id, organization_id, project_id, name, version, row_tier, seed, anomaly_spec_json, manifest_sha256, created_at)`
- `benchmark_cases(id, dataset_id, case_code, layer, category, is_p0_gate, is_p1_gate, input_json, expected_json)`
- `benchmark_runs(id, organization_id, project_id, dataset_id, layer, model_provider_version_id, policy_version_id, generator_version_id, is_mock_llm, p0_passed, p1_passed, overall_passed, raw_metrics_json, composite_scores_json, scoring_profile_version, started_at, ended_at)`
- `benchmark_case_results(id, benchmark_run_id, case_id, status, actual_json, metrics_json, error_message)`
- `model_provider_versions(id, organization_id, project_id, provider_name, base_url, model_name, config_hash, created_at)`
- `policy_versions(id, organization_id, project_id, version_tag, rules_hash, rules_json, approved_by, created_at)`
- `generator_versions(id, name, prompt_version, compiler_version, git_sha, created_at)`
