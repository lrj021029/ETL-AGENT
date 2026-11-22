# M7 & M8 数据面（Data Plane / SeaTunnel / Quality / Supervision / Publish / Rollback）实施记录

> **负责模块**：`@data-plane`  
> **覆盖里程碑**：M7 真实批处理链路、两阶段质量分流、运行监督、Doris 原子表替换发布与回滚 & M8 CDC 持续同步与 C+D 恢复策略  
> **状态**：代码、HOCON 模板及 L0/L1 契约与故障注入单元测试已完成（17/17 PASS）；L2 真实 Docker 容器（SeaTunnel Zeta 2.3.9 / MySQL 8.0 / PostgreSQL 16 / Doris 2.1.7）端到端执行待 Wave 1 合并并启动 Compose 全栈后执行验收。

---

## 1. 模块交付清单与文件所有权

本分支严格只在 `@data-plane` 独占目录内创建和修改文件，零触碰共享基线文档（`docs/PRD.md`, `docs/TECHNICAL_SPEC.md`）、`contracts/**`、`backend/migrations/**` 或其他 Agent 的目录：

| 目录 / 文件 | 核心职责 | 对齐契约 |
|---|---|---|
| `backend/app/connectors/models.py` | 连接器能力矩阵模型、状态枚举与错误码 (`CONNECTOR_CAPABILITY_UNSUPPORTED`, `FILE_PATH_TRAVERSAL_BLOCKED`) | `contracts/schemas/connector_registry.schema.json` |
| `backend/app/connectors/registry.py` | 版本化 `ConnectorRegistry`，预置 9 个 `CONFIGURED` 连接器与 3 个 `PLANNED` 连接器；严禁无证据虚标 `END_TO_END_VERIFIED` | `DEC-010`, `PRD-TBD-01`, `connector_registry.schema.json` |
| `backend/app/connectors/probe.py` | 只读元数据探查、确定性 SHA256 `SchemaFingerprint`、Drift 检测与受控本地文件目录边界 (`ControlledFileBoundary`) | `TECHNICAL_SPEC` §5.2, §5.5 |
| `backend/app/execution/state_machine.py` | 统一 `ExecutionRunRecord` 与 `BATCH` / `CDC` 分模式状态机守卫、确认态 (`REQUEST_SUBMITTED` / `ENGINE_CONFIRMED` / `RECONCILIATION_PENDING`) | `contracts/harness/execution_state_machine.yaml`, `realtime_events.yaml` |
| `backend/app/execution/diagnosis.py` | 零副作用故障根因诊断引擎 (`FailureDiagnosisEngine`, `advisory_only=True`, `side_effects_executed=False`) | `TECHNICAL_SPEC` §11.5 |
| `backend/app/quality/models.py` | 两阶段 `QualityContractModel`、7 类规则、3 种敏感原始值脱敏策略与质量报告记录实体 | `contracts/schemas/quality_contract.schema.json`, `ADR-004` |
| `backend/app/quality/engine.py` | Stage 1 流式行级分流器、SeaTunnel HOCON 分流构建器、Stage 2 影子表 `UNIQUE` 与受限 `SELECT` SQL AST 沙箱门禁 | `ADR-004`, `DEC-006`, `PRD-TBD-02` |
| `backend/app/supervision/engine.py` | `RuntimeSupervisionEngine`：严格按 `reject_rate > 0.20`（恰好 `0.20` 不触发）及滚动窗口、绝对错误数、四项预算与 CDC 延迟执行受管中断 | `contracts/schemas/runtime_supervision.schema.json`, `DEC-012`, `ADR-004` |
| `backend/app/publishing/doris_adapter.py` | `PublishAdapter` 协议与 `DorisPublishAdapter`：强制在生成/执行任何 DDL 前运行 Schema 兼容门禁，通过后执行 `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')` | `ADR-004` §3, `TECHNICAL_SPEC` §10.4, §13.1 |
| `backend/app/rollback/executor.py` | `RollbackExecutor`：受管执行 `SHADOW_CLEANUP`、`SWAP_RESTORE` 与遵循 **C+D 策略** 的 `CDC_RECOVER` | `DEC-010`, `TECHNICAL_SPEC` §10.5, §13.2 |
| `backend/app/data_plane/seatunnel_adapter.py` | `SeaTunnelZetaAdapter` REST 客户端（Zeta 2.3.9）：确定性 `stable_submit_id`、内存级 `${SECRET:...}` 物化、提交超时转 `UNKNOWN`（绝不盲目重提） | `TECHNICAL_SPEC` §5.3, §10.1, §10.3 |
| `backend/app/data_plane/reconciler.py` | `RunReconciler`：按 `stable_submit_id` 向 Zeta 对账并将 `UNKNOWN -> RECONCILING` 恢复至真实引擎状态 (`RUNNING` / `STREAMING`) | `TECHNICAL_SPEC` §10.3 |
| `backend/app/data_plane/worker_tasks.py` | `DataPlaneWorkerDispatcher`：从 PG `resolve_execution_tenant_context` 派生 RLS 上下文（绝不信任 Payload 租户 ID），并按 `command_id` 严格幂等 | `ADR-002`, `contracts/db/rls_policy_spec.md` §4, `outbox_events.schema.json` |
| `seatunnel/templates/*.conf` | 场景 A 宽表批处理、场景 B 日志清洗分流、MySQL CDC -> Doris、PostgreSQL CDC -> Doris 真实 Zeta 2.3.9 HOCON 模板（零明文密码） | `TECHNICAL_SPEC` §10 |
| `backend/tests/data_plane/test_*.py` | 17 项强制契约、边界、安全门禁与故障注入测试套件 | 6 大强制测试要求 |

---

## 2. 核心架构不变量验证说明（Unit/Fault-Injection vs. Live L2 区分）

1. **绝不以 Mock 单元测试冒充 L2 真实容器验收**：
   - 当前在 Worktree 阶段执行的 `pytest backend/tests/data_plane/ -v` 属于 **L0 契约校验 + L1 领域状态机/故障注入测试**。
   - 所有连接器在 `ConnectorRegistry` 中当前严格标记为 `CONFIGURED`（MySQL JDBC/CDC、PostgreSQL JDBC/CDC、Doris、MinIO/S3、Local CSV/JSON/Parquet）或 `PLANNED`（Oracle、ClickHouse、HTTP REST API），没有任何连接器被虚标为 `END_TO_END_VERIFIED`。
   - 待 Wave 1 合并、冷启动 Docker Compose 真实容器并跑通真实数据搬运后，方可调用 `ConnectorRegistry.record_verification_evidence(...)` 绑定真实 `test_evidence_ref` 与 `verified_at` 时间戳晋升状态。
2. **拒绝率 20% 严格大于（`>`）边界**：
   - `error_rows=200, validated_rows=1000` (`reject_rate == 0.20`) -> `breached=False`（不触发整体拒绝率熔断）。
   - `error_rows=201, validated_rows=1000` (`reject_rate == 0.201 > 0.20`) -> `breached=True`，触发 `HARD_INTERRUPT`，调用 `SeaTunnelZetaAdapter.cancel_job` 并生成只读诊断报告，等待 Operator 经 Harness 发起受管回滚。
3. **Doris 发布前强制 Schema 兼容门禁**：
   - 任何引擎非 OLAP、Key 模型/Key 列不一致、分区列不一致、列缺失/多余、数据类型或 Nullability 不匹配，均在生成或执行任何 SQL 前直接抛出 `PUBLISH_SCHEMA_INCOMPATIBLE`（`executed_sql_log` 为空）。
4. **CDC C+D 恢复策略**：
   - 历史检查点有效时（策略 C）从检查点恢复至 `RECOVERING -> STREAMING`。
   - 当处于 `CHECKPOINT_LOST` 或 `POSITION_EXPIRED` 且未传入 Operator 显式确认（`operator_confirmed_resnapshot=True` 及非空理由）时（策略 D），立即阻断并抛出 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`。
