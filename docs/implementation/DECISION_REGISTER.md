# 决策登记表（DECISION REGISTER）

> **维护者**：Lead / Principal Architect  
> **状态规则**：严格区分 `CONFIRMED`（PRD/Spec 原生确认）、`USER-APPROVED`（本轮用户明确拍板）、`PROPOSED`（技术建议）、`TBD`（仍未决，禁止静默假定为已确认）、`VERSION-CHECK`（需版本实测锁定）。

## 1. CONFIRMED 与 USER-APPROVED 决策

| ID | 领域 | 决策内容 | 状态 | 依据 |
|---|---|---|---|---|
| DEC-001 | 运行环境 | Windows 11 本机全量开发，主仓库 `d:\etl-agent`，使用本地 Docker (Kiri Linux Engine)，不管 18GB/20GB 阻塞配额，与 WSL 无关 | USER-APPROVED | 用户批复 #1/#2/#3 + `ENV-OVERRIDE-001` |
| DEC-002 | Compose 组织 | 单一 `compose.yaml` 默认启动全部核心服务 + MySQL + PG + Doris(1FE+1BE) + MinIO + Vault(持久化) + SeaTunnel Zeta(单节点) + Mailpit + 可观测性全栈；Oracle/ClickHouse/SeaTunnel 多节点作为同文件 `profiles` | USER-APPROVED | 用户批复 #3 + ADR-001 |
| DEC-003 | Vault 模式 | Vault 默认采用**持久化模式**（file/raft storage + 本地自动化 init/unseal 脚本，unseal 密钥入 `.gitignore` 绝不入库），同时提供 `SecretProvider` 抽象（Vault KV v2 + 加密 PostgreSQL Fallback Provider） | USER-APPROVED | 用户批复 #4 + ADR-001 |
| DEC-004 | Schedule 受管 | 引入 `StandingSchedulePreparation`（需经完整 Prepare + 审批并绑定固定 `PipelineVersion` 与预算）；Celery Beat 触发时经 Tool Broker 重验指纹/策略并签发当次 5 分钟单次 Ed25519 Capability + Outbox | USER-APPROVED | 用户批复 #5 + ADR-003 |
| DEC-005 | RLS 后台上下文 | Migration Role / Runtime Role / `outbox_dispatcher_role` 三角色分离；Runtime Role 无表所有权且无 `BYPASSRLS`；后台 Worker 经受控 `SECURITY DEFINER` 函数仅按主键解析 `(org_id, project_id)` 后立即 `SET LOCAL` 进入事务级 RLS | USER-APPROVED | 用户批复 #6 + ADR-002 |
| DEC-006 | Quality 两阶段 | `QualityContract` 采用两阶段执行：① SeaTunnel 流式行级分流（NOT_NULL/DATA_TYPE/REGEX/RANGE/ENUM → 影子表 + 错误表）；② 影子表级质量门禁（UNIQUE / 受限 SELECT 质量查询）在 `VALIDATING` 阶段执行后再进入 Doris Swap | USER-APPROVED | 用户批复 #6 + ADR-004 |
| DEC-007 | P3 安全边界 | P3 自动放行仅简化人工审批槽位，仍强制经过 Prepare 冻结 → Tool Broker 校验 → 5 分钟单次 Ed25519 Capability 签发与 PG 消费 → Outbox | USER-APPROVED | 用户批复 #6 + ADR-003 |
| DEC-008 | 实时协议 | 实时执行视图采用 **SSE + 轮询兜底**，前端封装可替换实时更新 Adapter | USER-APPROVED | 用户批复 #7 |
| DEC-009 | 本地认证 | JWT Access/Refresh + **Email OTP**（配本地 Mailpit SMTP 捕获，UI 标注“邮件二次验证”，不称强 MFA；TOTP/WebAuthn 列为后续增强） | USER-APPROVED | 用户批复 #7 + PRD §3.4 |
| DEC-010 | CDC 验收顺序 | M7 MySQL→Doris 批处理 → M8 MySQL CDC→Doris（首要 CDC 验收）→ M8 PostgreSQL CDC→Doris（独立集成验证），CDC 恢复严格采用 C+D 策略 | USER-APPROVED | 用户批复 #7 + PRD §8.5 |
| DEC-011 | 实施路线与协作 | 按 M0 → M9 有序里程碑实施，采用 Antigravity Swarm + 独立 Git Worktree 编排多 Agent 执行 | USER-APPROVED | 用户批复 |
| DEC-012 | 核心安全不变量 | Control/Data Plane 分离；Agent 零副作用；所有副作用经 Tool Broker；Immutable `PipelineVersion`；PG 唯一消费 + Redis 快路径；Evidence Ledger SHA256 链 + Ed25519 Checkpoint；`reject_rate = error_rows / validated_rows` 且 >20% 触发（恰好 20% 不触发） | CONFIRMED | PRD & Spec |

## 2. 仍保留为可配置 / TBD 的 P1/P2 参数（代码中必须参数化，禁止硬编码冒充已确认业务策略）

| TBD ID | 参数项 | 工程处理策略 |
|---|---|---|
| PRD-TBD-01 | Oracle / ClickHouse / REST API 深度适配优先级 | 纳入版本化 Connector Registry 标记为 `PLANNED`/`CONFIGURED`，首期真实 L2 端到端聚焦 MySQL/PG/Doris/MinIO/本地文件 |
| PRD-TBD-02 | 自定义 SELECT SQL 的扩展方言/复杂函数白名单、Transform 与 Quality 是否共享同一 AST 子集 | 严格默认拒绝未知 AST 节点与非白名单函数，白名单策略配置化并版本化 |
| PRD-TBD-05 | Preparation 指纹变化分类最终规则 | 策略引擎中采用显式配置表（重大变化默认 `INVALIDATE`，其余进入 `RE_EVALUATE`），不由 LLM 决定 |
| PRD-TBD-06 / 10 | Metadata 样本保留期、刷新周期、历史数据/日志保留天数 | 全部参数化为项目/环境策略配置项，默认保守最小留存并标注 `[CONFIGURABLE/TBD]` |
| PRD-TBD-08 | Reject Rate 最小样本量、滚动窗口长度、绝对错误行数上限 | 在 `RuntimeSupervisionContract` 中全部作为显式可配置字段；20% 整体阈值已确认（`>` 触发），其余参数必须显式传入或取环境级可配置安全默认值并标注 |
| PRD-TBD-11 | Schedule 默认 misfire / concurrency / 版本绑定 | 在 Schedule 创建时显式配置（提供安全推荐默认：`FORBID`、`SKIP`、固定 `pipeline_version_id`，API 可覆盖） |
| PRD-TBD-13 | Benchmark 三维综合评分权重与非 P0/P1 阈值 | 保留全部原始指标；评分器采用版本化权重配置文件（明确标注 `scoring_profile_version`），P0/P1 始终独立硬拦截 |
| PRD-TBD-14 | 执行预算数值、最大 SQL 时长、自动修复轮数上限 | 全部通过项目/策略配置驱动，代码强制检查上限非空且有界（防止无限循环），不在文档中伪称已锁定终值 |
| PRD-TBD-15 | 云端部署模板（K8s/Helm） | 当前交付以本地 Docker Compose 完整可运行闭环为准，云端模板留作后续扩展 |
