# Layer 0 契约：可观测性、Trace 关联与脱敏规范

- **Contract Version**：`1.0.0`
- **Owner**：Observability / Benchmark + Security / Harness + Lead

## 1. 四层可观测性分离
1. **Metrics (Prometheus)**：低基数数值时序指标。禁止把 `user_email`、`sql_text`、Secret、高基数行数据放进 Label。
2. **Logs (Loki)**：JSON 结构化运行日志，经脱敏过滤器输出。
3. **Traces (OTel Collector -> Tempo)**：跨 FastAPI → Outbox → Celery Worker → SeaTunnelAdapter 分布式追踪。
4. **Audit Evidence (PostgreSQL Evidence Ledger)**：哈希链 + Ed25519 Checkpoint；绝不以普通日志替代。

## 2. 统一关联字段（Log / Trace / Span Attributes）
- `trace_id`
- `organization_id`
- `project_id`
- `pipeline_id`
- `pipeline_version_id`
- `preparation_id`
- `execution_id`
- `external_job_id`

## 3. 标准 Prometheus 指标命名
- `etl_execution_read_rows_total` (labels: `execution_mode`, `connector_source`)
- `etl_execution_write_rows_total` (labels: `execution_mode`, `connector_sink`)
- `etl_execution_write_bytes_total` (labels: `execution_mode`, `connector_sink`)
- `etl_execution_error_rows_total` (labels: `execution_mode`, `error_code`)
- `etl_execution_reject_rate` (gauge, labels: `execution_id`, `window_type`)
- `etl_execution_amplification_ratio` (gauge, labels: `execution_id`)
- `etl_execution_duration_seconds` (histogram, labels: `execution_mode`, `status`)
- `etl_execution_budget_utilization_ratio` (gauge, labels: `execution_id`, `budget_type`)
- `etl_cdc_lag_milliseconds` (gauge, labels: `connector_source`, `execution_id`)
- `etl_harness_pdp_decisions_total` (counter, labels: `risk_level`, `decision`)
- `etl_harness_replay_blocked_total` (counter, labels: `source_component`)
- `etl_outbox_pending_events` (gauge)

## 4. 强制脱敏黑名单（日志、错误响应、LLM 外发、制品均生效）
任何键名匹配以下正则（不区分大小写）的值必须替换为 `"[REDACTED]"`：
`password|passwd|secret|token|api_key|apikey|private_key|authorization|cookie|access_key|secret_key|credential|dsn|connection_uri`
同时对匹配 `mysql://user:pass@`, `postgresql://user:pass@`, `Bearer [A-Za-z0-9._-]+`, `-----BEGIN .* PRIVATE KEY-----` 的字符串值做内容级脱敏。
