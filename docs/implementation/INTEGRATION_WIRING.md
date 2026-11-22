# ETL-Agent 控制面与数据面全链路集成装配说明（Integration Wiring Guide）

## 1. 概述

本文档记录 Wave 2 主干集成阶段（Integration Wiring）将 Wave 1 合并的 7 大领域模块无缝装配至 FastAPI 控制面与 Celery 数据面 Worker 的架构与实现细节。所有改动均遵循冻结契约（`contracts/**`）与中文注释规范，零绕过 Harness / Tool Broker / Capability 的外部副作用路径。

---

## 2. 核心装配文件清单

| 文件路径 | 职责说明 |
| --- | --- |
| `backend/app/api/wiring.py` | 组装 `WiringContainer`，统一初始化连接器能力矩阵、LangGraph 智能体、确定性门禁、制品冻结编译器、PDP 策略引擎、Preparation 冻结服务、Approval 双槽引擎、Ed25519 Capability 签发/验签与防重放守卫、Evidence Ledger 哈希链账本、Tool Broker、SeaTunnel Zeta 适配器、RunReconciler、Doris 发布适配器、回滚执行器、Benchmark 引擎与 Prometheus 指标注册表，并将 14 个处理器钩子注入 `app.state`。 |
| `backend/app/main.py` | 调用 `wire_application(app)` 完成启动装配；挂载 `observability.telemetry.mount_metrics_endpoint(app, "/metrics")`；启用 `security.redaction` 响应与错误脱敏防线；配置 `http://127.0.0.1:5173` 与 `http://localhost:5173` CORS 策略。 |
| `backend/app/api/routers.py` | 将全部 38 个 OpenAPI 契约端点与 `app.state` 中的真实领域服务钩子对接，消除静态占位返回。 |
| `backend/workers/celery_app.py` | 注册 7 类 Outbox 受管命令对应的 Celery 任务、未知状态对账任务（`reconcile_unknown_run`）、Outbox 轮询投递任务（`dispatch_pending_events`）与受管定时调度 Beat 任务（`evaluate_due_schedules`）。 |
| `backend/tests/integration/test_full_api_wiring.py` | 端到端 FastAPI `TestClient` 全链路集成验收测试套件。 |

---

## 3. 全链路装配拓扑

1. **可观测性与脱敏网关（Observability & Redaction）**：
   - `/metrics` 直接由 `backend.app.observability.telemetry.mount_metrics_endpoint` 导出 12 项契约指标。
   - HTTP 中间件自动分配/透传 `X-Trace-Id`，绑定结构化日志关联上下文，并在保护合法 `${SECRET:...}` 占位符与 `secret_ref_id` 引用结构的前提下清洗响应中的任何明文密码与凭据 URI。
2. **连接器能力矩阵（Connector Capability Matrix）**：
   - `/api/v1/connectors/registry` 绑定 `backend.app.connectors.registry.get_default_connector_registry()`，返回通过 `connector_registry.schema.json` 校验的 12 项版本化连接器条目（诚实标注 `CONFIGURED` 与 `PLANNED`，在未附带真实 L2 验收证据前绝不虚标 `END_TO_END_VERIFIED`）。
3. **LangGraph Agent 与确定性编译器（Agent & Compiler）**：
   - Agent 仅持有只读工具箱（`inspect_metadata_snapshot`, `list_connector_capabilities`），通过 `ContextVar` 在当前 RLS 事务内读取 `MetadataSnapshot`。
   - 缺参时触发 `interrupt` 挂起为 `INTERRUPTED`；恢复（`/resume`）时强制双重校验 `(user_id, organization_id, project_id, thread_id)` 归属，跨租户或跨用户恢复立即返回 403 `TENANT_ACCESS_DENIED`。
   - 候选产物经 `DeterministicGate` 校验后，通过 `POST /pipelines/{pipeline_id}/versions` 调用 `build_pipeline_version` 生成 JCS Canonical JSON 与 SHA256 `ArtifactManifest`，持久化不可变 `PipelineVersion`。
4. **Harness 安全治理与唯一副作用出口（Tool Broker）**：
   - `POST /preparations`：经 `PolicyEngine.evaluate` 执行风险分级（如 `prod` + `RESTRICTED_PII` 自动判定为 `P1` 并分配 `DATA_CHECKER` + `SECURITY_CHECKER` 双独立审批槽位；`P0` 直接硬拒绝 `PDP_DENIED_P0`）。
   - `POST /preparations/{id}/approvals`：强制执行 `MembershipService.enforce_approval_sod` 与 `ApprovalEngine.approve`，拦截 Maker 自批（403 `APPROVAL_SELF_APPROVE_FORBIDDEN`）与同一用户兼任双槽（403 `APPROVAL_SLOT_CONFLICT`）。
   - `POST /preparations/{id}/commit`：校验 `Idempotency-Key`，调用 `ToolBroker.commit` 签发并原子消费 5 分钟单次 Ed25519 `Capability` JWS，在同一数据库事务中写入 `CapabilityIssuance`、`CapabilityConsumption`、`ExecutionRun`、`ExecutionCommand`、`OutboxEvent`、`OutboxDelivery` 与 `EvidenceEvent`。
   - `POST /evidence/verify`：对当前租户哈希链自动补充或校验 Ed25519 签名检查点，调用 `verify_chain` 验证全链完整性。
   - `POST /rollbacks/prepare`：强制走 Harness `Preparation`（`preparation_type="ROLLBACK"`）与 PDP 审批槽位分配，严禁绕过 Harness 直接执行回滚 SQL。
5. **Celery Worker 数据面分发与幂等重投保护**：
   - Worker 任务通过 `SqlAlchemyControlPlaneRepository` 调用 `resolve_execution_tenant_context(execution_id, command_id)` 从数据库派生真实租户上下文并注入 `SET LOCAL` RLS，绝不信任消息体中的租户标识。
   - 相同 `command_id` 重投时立即返回 `IDEMPOTENT_NOOP`，绝不二次消费 Capability 或二次提交外部作业。
