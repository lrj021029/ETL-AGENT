# ETL-Agent 异构数据集成平台 — 项目面试深度 QA 手册

> **适用场景**：后端架构师 / AI Infra / Agent 工程 / 数据平台高级研发技术面试  
> **使用建议**：回答任何架构问题都坚持 **“背景痛点 → 核心设计权衡（Trade-off） → 边界与失效模式 → 真实代码与实测证据”** 四步结构，**切忌过度吹嘘**（诚实说明不做跨系统 ACID、Email OTP 非强 MFA、哈希链非外部 WORM，反而能极大赢得资深面试官信任）。

---

## 一、项目全局与架构选型（开场必问）

### Q1：请用 3 分钟介绍一下这个 ETL-Agent 项目，你在里面解决了什么核心问题？
**回答要点**：
- **业务背景**：传统异构数据集成（如 MySQL → Doris 宽表同步、日志清洗入仓、CDC 实时同步）需要数据工程师手写繁琐的 SQL、SeaTunnel HOCON 配置和质量规则，门槛高且变更容易引发线上故障；而直接把 LLM / Agent 接入数据库或调度引擎又极其危险（Prompt Injection、幻觉生成删库 DDL、无审批直接写线上表）。
- **核心架构**：我设计并实现了一套 **控制面 / 数据面严格分离 + 不可信 Agent 零副作用隔离** 的参考级平台：
  1. **前端与控制面**：Vue 3 + TypeScript（Vue Flow DAG、Monaco Editor/Diff、ECharts）+ FastAPI（38 条 OpenAPI 路由）+ PostgreSQL 16（71 张表 + `FORCE ROW LEVEL SECURITY`）。
  2. **零副作用 Agent 与确定性编译**：LangGraph 主图 + 7 个子图负责意图解析、只读探查、缺参中断澄清（PG Checkpoint）与候选生成；所有产出必须经过确定性门禁（`sqlglot` `SELECT`-only AST 沙箱、JSON Schema、Connector 能力矩阵、HOCON 校验）后冻结为不可变 `PipelineVersion`（RFC 8785 Canonical SHA256）。
  3. **Harness 安全内核**：任何副作用都必须经过 Tool Broker 的 `Prepare（PDP P0-P3 定级与指纹冻结） → Approve（Data/Security Checker 双独立审批，强制职责分离 SoD） → Commit（Operator 触发，单一 PG 事务内原子写入 5 分钟 Ed25519 Capability 单次消费 + ExecutionRun + Outbox + Evidence Ledger）`。
  4. **真实数据面与可观测性**：Outbox Dispatcher + Celery Worker 驱动 Apache SeaTunnel Zeta 2.3.9 执行真实 MySQL/PG → Doris 2.1.7 同步，采用两阶段质量治理（行级流式分流到影子表/错误表 + 影子表级 SQL/UNIQUE 校验）、>20% 拒绝率硬中断、Doris Schema 兼容门禁 + `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')` 原子发布与受管回滚，全链路接入 OTel / Prometheus / Loki / Tempo / Grafana。

### Q2：为什么采用 Control Plane / Data Plane 分离，而不是让 FastAPI 或 LangGraph 直接读 MySQL 写 Doris？
**回答要点**：
1. **稳定性与吞吐隔离**：FastAPI 与 LangGraph 属于轻计算、高并发 I/O 控制面，适合毫秒级 API、状态机与审批；百万行级数据搬运、Binlog/WAL 消费和内存缓冲必须下沉给专业分布式引擎（Apache SeaTunnel Zeta），避免数据任务 OOM 拖垮控制面。
2. **安全边界隔离**：控制面进程不直接持有目标数仓的高危 DDL/写入会话；凭据仅在 Celery Worker 执行数据面命令的最短生命周期内从 Vault KV v2 物化，且数据面集群（Zeta REST 5801、Doris BE）仅暴露在内部 Docker 网络，前端与 Agent 物理不可直达。
3. **可替换与可观测性**：通过 `SeaTunnelZetaAdapter` 与 `PublishAdapter` 抽象隔离引擎差异，控制面聚焦状态机、审计与对账。

---

## 二、Agent 安全、LangGraph 与确定性编译器（AI Agent 高频考点）

### Q3：如果用户输入恶意的 Prompt Injection，诱导 Agent 执行 `DROP TABLE` 或窃取敏感数据，你们怎么多层防御？
**回答要点（纵深防御 5 道防线）**：
1. **工具边界物理隔离**：`ReadOnlyToolRegistry` 在代码级与单测级断言 Agent 注册工具集**零写库、零 SeaTunnel 提交、零 Capability 签发、零 Commit/Rollback**；即使模型输出恶意 ToolIntent，也没有可执行入口。
2. **外发 LLM 脱敏与最小化**：`ProviderGateway` + `sanitizer.py` 默认仅向外部 LLM 发送必要 Schema 与脱敏样本，自动擦除密码、连接 URI、Bearer Token、PEM 私钥和未授权原始 PII 行，且不把模型隐藏 CoT 作为审计依赖。
3. **SQL `SELECT`-only AST 白名单沙箱**：绝不依赖正则黑名单！使用 `sqlglot` 解析为 AST，对节点类型、允许访问的表/列、允许函数进行**显式白名单校验**，任何 DDL/DML、多语句（`SELECT 1; DROP ...`）、系统库（`information_schema` / `pg_catalog` / `mysql.user`）、文件函数（`LOAD_FILE` / `INTO OUTFILE`）及未知 AST 节点一律 Fail-Closed 抛出 `SQL_AST_POLICY_VIOLATION`。
4. **确定性门禁与有界修复循环**：`DeterministicGate` 强制校验 Schema 对齐、SQL AST、HOCON 语法、`${SECRET:...}` 占位符与 Connector 能力矩阵；`RepairLoop` 受最大轮数上限约束，且**严禁 Agent 自行放宽任何策略或跳过门禁**。
5. **人类审批 + PDP P0 硬拒绝**：即便构造出合法制品，执行仍需通过 PDP（P0 硬拒绝不可审批放行）与双独立人类审批 + Operator Commit。我们用 L0 静态注入测试集（8 类攻击）验证 P0/P1 拦截率达到 **100%**。

### Q4：为什么把 SQL Transformation 和 SQL Quality Rule 分开建模？`ExecutionStrategyCompiler` 为什么不由 LLM 决定下推位置？
**回答要点**：
- **两类 SQL 语义与风险不同**：Transformation SQL 服务于字段映射、过滤与宽表关联，可能需要适度 JOIN/表达式；Quality Rule SQL 仅做数据质量断言，攻击面应更收窄（独立的 `SqlPolicyProfile`），且后者在两阶段质量架构的 Stage 2（影子表 `VALIDATING` 阶段）执行。
- **执行位置必须确定性可解释**：把转换放在 `SOURCE_PUSHDOWN`（源库下推）、`SEATUNNEL_TRANSFORM`（流中转换）还是 `TARGET_SIDE_SQL`（目标端影子表转换），直接影响源库 OLTP 压力、网络带宽与方言兼容性。由确定性 `ExecutionStrategyCompiler` 基于方言能力、表结构与资源预算计算，并输出明确的 `strategy_reason_codes` 固化在 `ArtifactManifest` 中，既可复现又方便审批人审查。

### Q5：LangGraph 缺参中断（Interrupt）与 Checkpoint 恢复时，如何防止跨租户恢复别人的 `thread_id`？
**回答要点**：
- 我们将 **Conversation State（对话）**、**Workflow State（LangGraph Checkpoint）** 和 **Execution State（`ExecutionRun`）** 三者严格分离。
- 每个 `thread_id` 都在受 PostgreSQL RLS 保护的 `agent_threads` 表中登记 `(organization_id, project_id, user_id, langgraph_thread_id)` 归属。
- 调用 `/agent/threads/{thread_id}/resume` 时，**先执行权限二次校验**（核对当前 JWT 主体、活跃 `organization_id`、`project_id` 归属以及当前双重成员资格是否被吊销），任何不匹配直接抛出 `TENANT_ACCESS_DENIED` (403)，绝不凭一个 `thread_id` 直接读取 Checkpoint。

---

## 三、Harness 安全内核、Capability 与 Evidence Ledger（后端安全核心考点）

### Q6：为什么需要 `Prepare → Approve → Commit` 三阶段？如果审批后元数据或密钥变了怎么办？
**回答要点**：
- **冻结审批事实，防止 TOCTOU（检查时到使用时竞态篡改）**：
  - `Prepare` 阶段零外部副作用，PDP 计算风险等级与审批槽位，并用 RFC 8785 Canonical JSON 计算 `PreparationFingerprint`（涵盖 `pipeline_version_hash`、`metadata_snapshot_fingerprint`、`connection_version`、`quality_contract_hash`、`runtime_contract_hash`、`policy_version`、`secret_versions`、`environment` 等）。
  - `Approve` 阶段，Data Checker 和 Security Checker 审阅的是不可变冻结事实，审批决策绑定 `preparation_id` + `fingerprint_sha256` + `slot_role`，并强制 **SoD（职责分离）**：Maker 不可自批（`APPROVAL_SELF_APPROVE_FORBIDDEN`）、同一人不可兼占双审批槽（`APPROVAL_SLOT_CONFLICT`）、自定义角色不可绕过系统 SoD。
  - `Commit` 阶段前，Tool Broker 重新实时计算当前输入指纹并调用确定性变更分类器（不由 LLM 决定！）。只要阻塞级字段发生变化，立即将准备单置为 `INVALIDATED` 并拒绝提交（`PREPARATION_INVALIDATED`），强制重新 Prepare & Approve。

### Q7：单次 Capability Token 怎么防重放？为什么不能只用 Redis？Commit 的事务边界如何设计？
**回答要点**：
1. **Ed25519 JWS + Vault Transit**：Capability 是短时有效（TTL=300s）的 Ed25519 签名 JWS（Header 含 `alg=EdDSA` 与 `kid` 支撑密钥轮转），完整绑定 15 个 Claims（`iss/sub/aud/jti/iat/exp/organization_id/project_id/environment/tool/preparation_id/pipeline_version_hash/approval_digest/policy_version/nonce`）。生产/持久化环境通过 **HashiCorp Vault Transit** 签名（私钥永不出 Vault），并将 Vault 返回的 `vault:v1:<标准Base64>` 64 字节原始签名转为标准 JWS Base64URL。
2. **为什么 Redis 不能做唯一事实来源**：Redis 存在主从切换丢 key、内存淘汰或重启清空风险，因此我们采用 **“Redis `SET etl:cap:jti:<jti> 1 NX EX 360` 快速拦截 + PostgreSQL `capability_consumptions(jti UNIQUE)` 唯一约束作为最终持久事实”** 的双层防重放设计。即使 Redis 被清空，PostgreSQL 唯一约束依然在并发竞态下保证只有 1 次 Commit 成功。
3. **关键事务边界（解决 At-least-once 重复投递冲突）**：在 `ToolBroker.commit()` 的**单一 PostgreSQL 事务**中，原子写入 7 类记录：`capability_issuances`、`capability_consumptions(jti)`、`execution_runs`、`execution_commands(command_id UNIQUE)`、`outbox_events`、`outbox_deliveries`、`evidence_events`。后续 Outbox Dispatcher 投递至 Celery 若发生消息重投，Worker 校验签名绑定与 `command_id` 幂等即可，**绝不把同一命令重投当成第二次 Capability 消费**。

### Q8：定时任务（Cron Schedule）无人值守触发，怎么既能自动跑又不绕过 5 分钟单次 Capability？
**回答要点**：
- 这是规格里一个非常典型的架构冲突。我们在 `ADR-003` 中引入了 **`StandingSchedulePreparation`** 机制：
  1. 创建/变更 Schedule 时，必须绑定**固定不可变的 `pipeline_version_id`**、明确的执行预算、并发策略（默认 `FORBID`）与错过策略（默认 `SKIP`），并走完完整的 `Prepare + 对应风险等级审批`。
  2. Celery Beat 到点触发时，不是直接调用 SeaTunnel，而是以受限系统主体 `system:scheduler` 调用 Tool Broker：实时重验 `StandingSchedulePreparation` 未失效、无重大指纹漂移、满足并发与预算配额后，**当场签发一张新的 5 分钟单次 Ed25519 Capability**，并走完全一致的单事务 Commit + Outbox + Evidence 链路。
- 同样地，P3 低风险自动放行也仅简化人工审批槽位，**仍强制走 Prepare + Tool Broker + 单次 Capability + Outbox**。

### Q9：Evidence Ledger 怎么做防篡改？它能防住拥有 PostgreSQL 超级管理员权限的人吗？
**回答要点**：
- `evidence_events` 表按 `(organization_id, project_id, sequence_no)` 严格单调递增追加，业务层不提供任何 UPDATE/DELETE API。每条事件按固定字节布局计算哈希：`event_hash = "sha256:" + SHA256(jcs_bytes(event_without_hash) + "\n" + previous_hash)`，创世 `previous_hash` 为 64 个 `0`。
- 每隔 N 条事件生成 `evidence_checkpoints`，由 Vault Transit Ed25519 私钥签名并记录 `kid`。校验器可一键检测单行篡改、删中间行、乱序重排和伪造检查点签名。
- **诚实边界（加分点）**：如果攻击者同时掌握 PostgreSQL 超级管理员权限，理论上可以整体重写历史表数据，但因为没有 Vault Transit 私钥，伪造不出合法的 Ed25519 Checkpoint 签名；不过若对方直接把整张表删光，本地 DB 自身无法阻止——所以我们诚实声明它是**可检测篡改的 L2 审计证据链**，而非外部 WORM 不可删除存储（如需 WORM 可通过后续外部对象存储 Object Lock 锚定 Checkpoint）。

---

## 四、多租户隔离与 PostgreSQL RLS（数据库深度考点）

### Q10：你们怎么实现应用层 + PostgreSQL RLS 双重多租户隔离？连接池复用会串租户吗？后台 Worker 怎么查数据？
**回答要点**：
1. **三类数据库角色物理分离（`ADR-002`）**：
   - `etl_migrator`：仅限 Alembic 迁移与表拥有者（Table Owner），应用运行时严禁使用。
   - `etl_runtime`：FastAPI 与 Celery Worker 使用，**不拥有任何业务表，明确 `NOBYPASSRLS` + `NOSUPERUSER`**。
   - `etl_outbox_dispatcher`：仅对 `outbox_events` 和 `outbox_deliveries` 有最小读写权，无权读取任何业务表。
2. **防连接池串租户**：所有租户表全部开启 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`，谓词采用 **Fail-Closed**（缺少 `app.organization_id` 或 `app.project_id` 返回 `FALSE`）。每个请求在开启事务后立刻执行 `SELECT set_config('app.organization_id', :org_id, true)`——第三个参数 `true` 等价于 `SET LOCAL`，事务提交或回滚后变量自动清零，连接归还 SQLAlchemy 连接池后绝无残留。
3. **解决 `FORCE RLS` 下的两大“鸡生蛋”难题**：
   - **Celery Worker 后台任务**：绝不信任消息体里的 `tenant_id`！我们在迁移中定义了仅授予 `etl_runtime` 的受控 `SECURITY DEFINER` 函数 `resolve_execution_tenant_context(execution_id, command_id)`，仅凭主键返回持久化 `(organization_id, project_id)`，Worker 拿到后立即 `SET LOCAL` 进入严格 RLS 事务再校验命令与业务表。
   - **登录后尚未选组织调用 `/api/v1/auth/me`**：同理，通过迁移 `0003` 提供最小暴露面 `SECURITY DEFINER` 函数 `resolve_user_organizations / projects / role_bindings(p_user_id)`，仅返回已认证用户自己的成员归属，同时所有业务表直查依然保持 100% Fail-Closed。
4. **验证手段**：严禁用 SQLite 代替测 RLS！我们在真实 PostgreSQL 16 容器上运行了全部 5 项负向隔离攻击测试（缺上下文、跨组织、同组织跨项目、连接池连续复用、`etl_runtime` 角色特权检查）。

---

## 五、数据面一致性、SeaTunnel、Doris 原子发布与 CDC

### Q11：Outbox、Celery、SeaTunnel 和 Doris 跨 4 个系统，如何保证不重复写数据？你们承诺 Exactly-once 吗？
**回答要点**：
- **首先明确不承诺跨异构系统的通用 ACID 或全局 Exactly-once**，而是用确定性状态机 + 幂等 + 对账 + 补偿收敛一致性：
  1. **Outbox → Celery**：`OutboxDispatcher` 用 `SELECT ... FOR UPDATE SKIP LOCKED` + 租约投递（At-least-once）。
  2. **Worker 消费**：以持久化 `command_id` 唯一约束做幂等保护，重复投递直接返回既有结果，不二次触发外部提交。
  3. **SeaTunnel 提交响应丢失（最难场景）**：调用 Zeta REST `submit-job` 前，Worker 先持久化 `status=SUBMITTING` 与确定性 `stable_submit_id`（作为作业名）。如果 HTTP 超时或 Worker 崩溃导致响应丢失，状态转入 **`UNKNOWN`**（UI 显示“状态待对账”），**严禁自动盲目重提**！由 `RunReconciler` 查询 Zeta 集群运行/完成作业列表按 `stable_submit_id` 对账，确认已建作业则平滑接管，未建才由受管恢复决定。
  4. **目标端幂等与原子发布**：批处理写入独立影子表 `<target>__shadow_<short_exec_id>`，通过后再原子 Swap，天然免疫中途重试产生的脏数据；CDC 依据具体 Connector 与 Sink（如 Doris Unique Key Upsert）诚实声明语义。

### Q12：数据质量分流和 >20% 拒绝率中断怎么设计？为什么 Doris 原子替换前必须自己做 Schema 兼容检查？
**回答要点**：
- **两阶段 QualityContract（`ADR-004`）**：
  - **Stage 1（流式行级分流）**：`NOT_NULL`、`DATA_TYPE`、`REGEX`、`RANGE`、`ENUM` 等无状态行级规则在 SeaTunnel 作业内将合法行写入影子表、违规行连同 `rule_id`、`error_code`（`DQ_*`）、脱敏原因写入错误表。
  - **Stage 2（影子表级校验）**：`UNIQUE` 全局判重与受限 `SELECT` 质量查询无法在无状态行级算子完成，在落盘影子表后的 `VALIDATING` 阶段执行并汇总最终 `QualityReport`。
- **精确监督阈值语义**：`reject_rate = error_rows / validated_rows`；整体阈值 `20%` 按字面 **严格大于（`>`）** 触发（实测边界：`200/1000 = 20.0%` 不触发，`201/1000 = 20.1%` 立即触发 `HARD_INTERRUPT` 调 `cancel_job`），同时叠加可配置的最小样本量 `min_validated_rows`、滚动窗口拒绝率、绝对错误行数与读行/写字节/时长/放大比预算。中断后仅生成**零副作用诊断建议**，等待 Operator 经 Harness 审批回滚。
- **Doris 原子 Swap 陷阱**：Apache Doris 2.1 的 `ALTER TABLE <target> REPLACE WITH TABLE <shadow> PROPERTIES('swap'='true')` 是元数据级原子切换（且保留旧表数据为归档表支撑回滚），但 **Doris 官方明确不校验两张表的 Schema 是否一致**！如果影子表字段类型或列序漂移，直接 Swap 会导致下游查询报错或静默错位。因此 `DorisPublishAdapter` 在执行任何 DDL 前强制执行 Schema 兼容门禁（校验 OLAP 引擎、列名、数据类型、Nullability、Key 模型与分区），不兼容直接抛出 `PUBLISH_SCHEMA_INCOMPATIBLE`。
- **双版本解耦**：`PipelineVersion` 表示“代码/配置逻辑版本”，`PublishedDatasetVersion` 表示“目标表实际数据发布版本（含 `previous_version_id`、`archive_table_ref`）”，回滚是面向 `PublishedDatasetVersion` 的受管 Swap 恢复，并新建 `rollback_runs` 与审计事件，永不删历史。

### Q13：CDC 场景下遇到 Checkpoint 丢失或 MySQL Binlog / PG Replication Slot 过期怎么办？
**回答要点**：
- 遵循 **C + D 恢复策略**：
  1. 故障发生后先检测并尝试从最近可用历史 Checkpoint 恢复（C）。
  2. 一旦检测到 `CHECKPOINT_LOST` 或 `POSITION_EXPIRED`（如 Binlog 已被清理、PG Slot 失效），系统**严禁静默丢弃位点或自动无告警全量重跑**（全量重跑可能打爆源库 OLTP 或在目标端产生重复/漏删数据），而是立即阻断并抛出 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`，由 Operator 评估风险后经 Harness 显式确认是否执行重新快照或重建 Slot（D）。

---

## 六、真实工程踩坑与排障复盘（面试极高含金量加分项）

### Q14：在这个项目从 0 到 1 实机联调过程中，你踩过哪些最印象深刻的真实坑？怎么解决的？
**真实踩坑清单（每一个都在本仓库实机验证并有对应 commit 证据）**：
1. **PostgreSQL `NOSUPERUSER` 角色跑 Alembic RLS 迁移报错**：为了安全，`etl_migrator` 明确设为 `NOSUPERUSER`，但初版 `0002_rls_policies.py` 对已存在的 `etl_runtime` 执行了 `ALTER ROLE ... NOSUPERUSER`，被 PG 16 直接拒权。**解决**：改为 `DO $$` 块查询 `pg_roles` 断言 `rolbypassrls = false AND rolsuper = false`（若被篡改则 `RAISE EXCEPTION` 阻断部署），既保安全又兼容非超管迁移。
2. **`FORCE ROW LEVEL SECURITY` 导致 `/api/v1/auth/me` 返回空组织列表**：在 SQLite 单测下完全正常，一上真实 PostgreSQL 16 容器就暴露了——用户登录后还没选 `X-Organization-Id`，`FORCE RLS` 把用户自己的 `organization_members` 全部 fail-closed 过滤成空。**解决**：新增迁移 `0003` 受控 `SECURITY DEFINER` 函数仅按已认证 `user_id` 解析自身归属，既保持全部业务表 100% Fail-Closed，又打通登录初始化。
3. **Apache Doris 2.1.7 在 Docker/Kiri 环境下重启后失联与 BE 启动三连坑**：
   - 坑 1：BE `entry_point.sh` 强校验 `FE_SERVERS`/`BE_ADDR` 必须为 IPv4（不接受主机名）→ 在 entrypoint 动态解析或固定 IP。
   - 坑 2：BE 启动强检查 `vm.max_map_count >= 2000000` 与禁用 swap → 容器内前置 `sysctl -w vm.max_map_count=2000000` 与 `swapoff -a`。
   - 坑 3：宿主机重启后 Docker Bridge 给 `doris-fe` 分配了新 IP，导致 BDBJE 元数据选主死循环（`feType:UNKNOWN`）→ 在 `compose.yaml` 为 `etl-internal` 声明固定子网并为 FE/BE 固定 `ipv4_address`，重启后秒级恢复 Quorum。
4. **Windows PowerShell 5.1 写 UTF-8 BOM 导致 Python 解析 Vault Token 失败**：`init-vault.ps1` 保存 `vault-init.json` 时 PowerShell 5.1 默认带 UTF-8 BOM，导致 Python `json.loads` 报 `Unexpected UTF-8 BOM` 并引发 Vault 403。**解决**：Python 读取端统一用 `encoding="utf-8-sig"`，彻底兼容 Windows 跨工具链。
5. **Grafana 只读父目录嵌套挂载失败**：在 `:ro` 的 `/etc/grafana/provisioning` 下嵌套挂载 `/etc/grafana/provisioning/dashboards/json` 触发 `Read-only file system (os error 30)` → 将 dashboard JSON 挂载至独立路径 `/var/lib/grafana/dashboards:ro` 并同步 `dashboards.yaml`，5 套看板全部自动加载。
6. **加密单测偶发抖动的定位**：测试 Fernet 加密不暴露明文时，初版明文只有 2 字节 `"v2"`，随机 Base64 密文约有 3% 概率碰巧包含子串 `v2` 导致 CI 偶发红灯 → 替换为 18 字节高熵测试明文，消除统计碰撞实现 0 抖动。

---

## 七、Benchmark 与可观测性追问

### Q15：你们怎么评估 ETL-Agent 的效果？为什么把 P0/P1 安全门槛设为独立一票否决？
**回答要点**：
- **三层评测金字塔**：`L0`（静态安全注入：Prompt Injection、SQL 注入、越权 ToolIntent、PII 外发、Capability 重放、路径穿越、自批）、`L1`（故障注入：源库断连、Worker 崩溃重投、提交丢响应 `UNKNOWN`、20.0% vs 20.1% 边界、Schema Drift、CDC 位点丢失）、`L2`（真实 Docker 全栈链路）+ `10K / 100K / 1M` 固定种子确定性数据集（可复现 SHA256 清单）。
- **独立硬门槛原则**：如果用加权总分把“生成准确率 99%”和“安全拦截率”混在一起，一次 P0 删库放行可能被高准确率掩盖成“总分 92 分通过”，这在数据安全场景是灾难性的。因此 `benchmarks/runner/scoring.py` 保留全部 11 项原始指标，三维综合分权重明确标记为可配置 Profile，而 **任意 1 条 P0 或 P1 安全用例失败，直接强制 `overall_passed = False`**；同时严禁把 Mock LLM 与真实 LLM 评测混在一起统计。
