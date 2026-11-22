# M1 可观测性栈与 M9 确定性基准测试引擎实施说明

## 1. 可观测性栈（M1）

### 1.1 配置文件清单
- `observability/prometheus/prometheus.yml`：抓取 `api:8000/metrics`、`otel-collector:8889`、`tempo:3200`、`loki:3100` 与自身指标。
- `observability/otel/otel-collector-config.yaml`：接收 OTLP gRPC (`4317`) / HTTP (`4318`)，经 `memory_limiter` 与 `batch` 处理器路由至 Tempo、Prometheus 与 Loki。
- `observability/loki/loki-config.yaml`：Loki 3.3.2 单机配置，启用 `allow_structured_metadata` 以支持 `trace_id` 等关联字段检索。
- `observability/tempo/tempo-config.yaml`：Tempo 2.6.1 单机配置，接收 OTLP 链路数据并本地存储。
- `observability/grafana/provisioning/datasources/datasources.yaml`：预置 Prometheus、Loki 与 Tempo 数据源，并配置 `trace_id` 双向关联跳转（`derivedFields` 与 `tracesToLogsV2`）。
- `observability/grafana/dashboards/*.json`：5 套看板（Platform Health、API & Worker、SeaTunnel Data Plane、Quality & Supervision、Security & Benchmark），指标名严格对齐 `contracts/observability/telemetry_spec.md`。

### 1.2 应用侧遥测与脱敏（`backend/app/observability/`）
- `logging.py`：提供 `StructuredJsonFormatter`、`correlation_context` 与 `redact_secrets` / `redact_string`，对 8 个标准关联字段自动注入，并对密钥键名、数据库 DSN、Bearer Token、PEM 私钥执行强制 `[REDACTED]` 脱敏。
- `telemetry.py`：提供 `MetricsRegistry`（导出全部 12 个标准 Prometheus 指标）、`/metrics` ASGI 挂载助手（`mount_metrics_endpoint`）及 OpenTelemetry SDK 初始化封装（`init_telemetry`）。

## 2. 确定性基准测试引擎（M9）

### 2.1 数据集生成器（`benchmarks/generator/dataset_generator.py`）
- 支持 `10K`（10,000）、`100K`（100,000）、`1M`（1,000,000）三档流式生成，采用固定随机种子与固定基准时间戳，确保跨运行字节级确定性与 SHA256 清单一致。
- Scenario A（`orders_users`）：Orders + Users 宽表。
- Scenario B（`logs_anomaly`）：支持可控错误注入率（默认 `0.25` > 20%），覆盖 `DQ_NOT_NULL_VIOLATION`、`DQ_DATA_TYPE_VIOLATION`、`DQ_REGEX_VIOLATION`、`DQ_RANGE_VIOLATION`、`DQ_ENUM_VIOLATION`、`DQ_UNIQUE_VIOLATION` 与 Schema Drift。

### 2.2 评测编排与评分（`benchmarks/runner/`）
- `engine.py`：编排 L0（静态安全注入 `l0_security_cases.json`）、L1（故障模拟 `l1_fault_cases.json`）与 L2（真实端到端接口）。
- `scoring.py`：保留全部 11 项原始指标；按可配置三维权重（带 `scoring_profile_version` 标识，不硬编码冒充终值）计算综合分；严格执行 **P0/P1 独立硬门槛**（任意 P0/P1 失败直接判定 `overall_passed = False`）；严禁混合 `mock` 与 `real` 运行结果。
