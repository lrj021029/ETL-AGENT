# M4 确定性编译器 / SQL 沙箱 / Gate 与 M5 LangGraph StateGraph 实现说明

## 模块布局

| 目录 | 职责 |
| --- | --- |
| `backend/app/artifacts/` | RFC 8785 (JCS) 兼容的 Canonical JSON + SHA256（逐字节通过 `contracts/crypto/test_vectors.json`），以及排除动态时间戳的不可变 `ArtifactManifest` / `PipelineVersion` |
| `backend/app/pipeline/` | `EtlPlan` / `QualityContract` / `RuntimeSupervisionContract` 的 Pydantic v2 模型、Vue Flow DAG 构建器、Schema Diff 与敏感列脱敏预览 |
| `backend/app/sql_analysis/` | 基于 `sqlglot` 的严格 `SELECT`-only SQL AST 安全沙箱（节点/表/列/函数显式白名单；Transformation 与 Quality Rule 双 Profile；违规统一 `SQL_AST_POLICY_VIOLATION`） |
| `backend/app/compiler/` | 确定性 `ExecutionStrategyCompiler`（`SOURCE_PUSHDOWN` / `SEATUNNEL_TRANSFORM` / `TARGET_SIDE_SQL` + 显式原因码）与不可变规则集的 `DeterministicGate` |
| `backend/app/hocon/` | 确定性 SeaTunnel HOCON 生成器（含两阶段质量流式拆分配置，仅输出 `${SECRET:...}` 占位符）与结构校验器 |
| `backend/app/agent/` | LangGraph 主 `StateGraph` + 7 个子图、只读工具注册表、外部 LLM 载荷脱敏器、`OpenAICompatibleProvider` + 显式标记 `is_mock_llm: true` 的 `DeterministicMockProvider` |

## 关键安全与确定性不变量

1. **LLM 从不直接决定执行落位、连接绑定或密钥引用**：这些字段全部由确定性代码从受控上下文与 HOCON 占位符集合推导。
2. **只读工具边界**：`ReadOnlyToolRegistry` 在注册与服务启动时双重断言 `side_effect == "READ_ONLY"` 且名称不含任何写/提交/签发/提交/回滚动词。
3. **跨租户与权限二次校验**：澄清恢复时校验 `(user_id, organization_id, project_id, thread_id)` 与线程归属完全一致，并重新调用 `PermissionChecker`；不符则抛 `TENANT_ACCESS_DENIED` 且不触碰 checkpoint。
4. **有界 `RepairLoop` 绝不放宽门禁**：`DeterministicGate` 位于图状态之外；耗尽 `max_repair_attempts` 后返回 `REPAIR_LOOP_EXHAUSTED` 并交还 Maker。
5. **外送脱敏与无隐藏 CoT 存储**：`ProviderGateway` 在调用前剥离敏感键、URI/JDBC 串与未批准样本行，并在返回后丢弃任何推理链字段。
