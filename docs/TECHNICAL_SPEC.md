# 生产级 ETL-Agent 数据集成平台 — TECHNICAL SPEC

> 文档类型：技术设计规格说明书
> 版本：v1.1（单机 Windows 11 Pro本地全栈开发基线）
> 状态：架构基线；接口细节、阈值和部分部署参数以 `TBD` 标注
> 日期：2026-11-22

---

## 0. 设计约定

本文使用以下标记：

* **CONFIRMED**：用户在需求访谈及环境确认中明确选择。
* **PROPOSED**：为使方案可实现而提出的技术设计建议，尚未视为用户确认。
* **TBD**：信息不足，禁止在实现中静默假设。
* **VERSION-CHECK**：依赖具体 SeaTunnel / Connector / Doris / Vault / PostgreSQL 版本，必须先验证兼容性再锁定。

本项目是高完成度作品集参考实现，不自动等同于通过生产环境认证。安全设计采用生产系统的边界与失效处理方式，但部署规模和高可用承诺以已验收的范围为准。

## 1. 系统架构

### 1.1 总体原则

1. **Control Plane / Data Plane 分离**：FastAPI、LangGraph、PostgreSQL、Redis、Celery 和 Harness 管理控制面；SeaTunnel Zeta 承担数据搬运。
2. **Agent 不可信**：LLM 输出视为不可信数据；Agent 仅生成候选、执行受管只读探查和确定性校验，不持有直接写库或提交任务权限。
3. **所有副作用经 Broker**：数据库写入、SeaTunnel 提交/停止、影子表操作、回滚均经 Tool Broker + PDP + Capability 流程。
4. **Immutable Artifact**：PipelineVersion 是不可变执行制品；运行时不得从 Studio 的未冻结草稿读取执行配置。
5. **持久事实优先**：PostgreSQL 是控制面业务事实、审批、Checkpoint、Capability 消费、ExecutionRun 和 Outbox 的持久化来源。
6. **跨系统不假设 ACID**：SeaTunnel、Redis、Celery 和目标数据库之间通过幂等、Outbox、状态机、对账和补偿管理一致性。
7. **显式能力边界**：连接器支持矩阵、SQL 子集、发布能力和 CDC 恢复能力按版本记录，不以一个“支持”布尔值覆盖差异。

### 1.2 逻辑组件

```text
Windows 11 Pro (32GB RAM Host)
  ├─ Windows Desktop GUI (Antigravity Client + Browser)
  │     |
  │     | WSL Remote IPC (wsl.exe / Remote-WSL)
  │     v
  └─ Windows (Windows Development & Execution Host)
        ├─ Git repository / Git worktrees (~/workspace/etl-agent)
        ├─ Antigravity remote agent server & terminals
        ├─ Docker CLI
        └─ Docker Desktop kiri Engine (18GB Memory Cap)
              |
              +------------------- Docker Compose --------------------+
              |                                                        |
              v                                                        v
        Vue 3 Console                                            Observability
              |                                               Prometheus/Grafana
              v                                               Loki/OTel/Tempo
        FastAPI Control Plane
          ├─ Auth / Org / Project / RBAC+ABAC
          ├─ Connection & Metadata Service
          ├─ Pipeline Artifact Service
          ├─ LangGraph Orchestrator
          ├─ PDP / Preparation / Approval
          ├─ Capability Broker / Replay Guard
          ├─ Evidence Ledger
          ├─ Runtime Supervision
          └─ API / WebSocket / SSE (transport choice TBD)
              |
              +---------------- PostgreSQL -----------------------------+
              |  Business facts, RLS, Checkpoints, Outbox, Ledger         |
              |
              +---------------- Redis ----------------------------------+
              |  Celery broker/backend, cache, replay fast-path           |
              |
              +---------------- Outbox Dispatcher -----------------------+
                                  |
                                  v
                            Celery Worker
                              ├─ SecretProvider / Vault
                              ├─ SeaTunnel Adapter
                              ├─ Run Reconciler
                              └─ Runtime Supervisor
                                  |
                                  v
                          SeaTunnel Zeta Cluster
                            ├─ Source Connectors
                            ├─ Transform
                            └─ Sink Connectors
                                  |
                                  v
                         Shadow / Error / Target Tables
                                  |
                                  v
                          Publish Adapter / Swap

```

### 1.3 确定技术选择

| 层 | 选择 | 状态 |
| --- | --- | --- |
| 前端 | Vue 3 + TypeScript + Tailwind CSS + shadcn-vue | CONFIRMED |
| DAG | Vue Flow | CONFIRMED |
| 代码编辑 | Monaco Editor / Diff Editor | CONFIRMED |
| 图表 | Apache ECharts | CONFIRMED |
| API | FastAPI | CONFIRMED |
| Agent | LangGraph 主 Graph + 子 Graph | CONFIRMED |
| Model Provider | OpenAI-compatible API | CONFIRMED |
| Workflow Checkpoint | PostgreSQL | CONFIRMED |
| Task Queue | Celery | CONFIRMED |
| Broker / Cache | Redis | CONFIRMED |
| Control DB | PostgreSQL | CONFIRMED |
| Secrets | Vault KV v2 + 加密存储 Provider 抽象 | CONFIRMED，细节 TBD |
| Signing | Ed25519；本地开发密钥，部署环境 Vault Transit | CONFIRMED |
| Data Engine | Apache SeaTunnel Zeta | CONFIRMED |
| Target Demo | Apache Doris | CONFIRMED |
| Object Storage | MinIO / S3 | CONFIRMED |
| Metrics | Prometheus | CONFIRMED |
| Dashboards | Grafana | CONFIRMED |
| Logs | Loki | CONFIRMED |
| Traces | OpenTelemetry + Grafana Tempo | CONFIRMED |
| CI | GitHub Actions | CONFIRMED |

具体版本不得凭记忆指定。采用版本兼容矩阵验证 Python、Node、PostgreSQL、Redis、Celery、LangGraph、SeaTunnel、Connector、Doris、Vault 和容器镜像，再提交锁定文件。

---

## 2. Windows 11 Pro + WSL2 本地全栈开发与运行环境

### 2.1 运行拓扑

**CONFIRMED 运行边界：** 全部开发与运行均在单台 **Windows 11 Pro** 主机上完成；Windows 桌面端运行 **Antigravity IDE GUI** 与浏览器，代码、终端、多 Agent 进程与 Docker 容器均在本机环境中执行。




### 2.3 本机网络与端口隔离

* docker默认支持 `localhost` 自动转发（`localhostForwarding=true`），Windows 浏览器可直接访问暴露出的前端控制台端口（如 `http://localhost:5173`）与 Grafana 入口。
* 内部基础设施（PostgreSQL、Redis、Vault、SeaTunnel REST/Hazelcast、Doris BE 内部端口）默认仅在 Docker Compose 内部网络（Bridge Network）可达，不向外部局域网暴露。
* 若需在 Windows 宿主直接调试特定控制面 API，仅绑定 `127.0.0.1` 回环地址，严禁暴露 Docker Daemon 管理端口。

### 2.4 单一工作区与 Git Worktree

。GitHub 仅用于远程备份、分支推送、PR 审查和 GitHub Actions CI。

多 Agent 协作使用独立 Git Worktree 与独立特性分支（如 `~/workspace/etl-agent-wt/<agent-role>`）；严禁多个 Agent 无约束地并发修改同一工作树。具体文件所有权在开发任务表中定义。

### 2.5 资源预算

**CONFIRMED 硬件与配额：**

* Windows 物理内存：32GB。
* Docker 最大内存限制：**18GB**（剩余 14GB 留给 Windows 11 Pro 宿主、Antigravity GUI 客户端、多个并发 Agent 进程、浏览器及 WSL2 系统内核；建议在 `C:\Users\<user>\.wslconfig` 中将 WSL2 总内存上限设为 `20GB` 以预留 2GB Linux 系统缓冲）。
* 可用磁盘：200GB。

单 Compose 文件且服务默认启动为已确认约束。在 18GB Docker 内存边界下，Doris FE/BE、SeaTunnel JVM、控制面服务与可观测性组件同时运行存在内存压力，必须执行以下硬性保障：

1. 为所有容器显式配置 `mem_limit` / `cpus` 以及 JVM 服务（SeaTunnel、Doris FE/BE）的 `-Xms`/`-Xmx` 上限（具体分配比例压测后锁定）。
2. 对 Docker Volume、日志、Benchmark 数据和 Doris 数据目录设置磁盘占用监控与告警。
3. 实现 `docker compose up -d` 的全服务冷启动基线测试。
4. 记录启动后稳定内存、峰值内存、磁盘增长和 10K / 100K / 1M 数据集运行消耗。
5. 如果 18GB 无法满足全服务默认稳定运行，必须返回架构评审，不得静默移除服务或默认禁用服务。

### 2.6 Compose 组织

用户选择单个 Compose 文件、所有服务默认启动。默认 Compose 应包含核心服务和选定的真实演示数据服务。多节点 SeaTunnel、更多 Doris 节点、持久化 Vault 或可选的重型数据库可采用同文件扩展配置，但是否通过 Compose Profile 管理属于 `TBD`，不能把已选择的“默认全启动”悄然改成“核心默认启动”。

默认 SeaTunnel 为单节点；多节点配置作为扩展。默认 Doris 为单 FE + 单 BE；扩展集群作为后续配置。Vault 同时提供 Dev 与持久化模式，默认模式与启动命令的选择需在部署 ADR 中明确。

---

## 3. 服务边界与模块职责

### 3.1 Frontend

* Auth / Organization / Project 页面。
* Connection / Metadata Profile 页面。
* Pipeline Studio、DAG、Monaco HOCON/SQL、Diff。
* Preparation、Approval Console。
* Execution Center、Quality Report、Rollback UI。
* Security Governance、Benchmark Dashboard。
* 实时执行视图通过 WebSocket 或 SSE + 轮询兜底；优先协议尚未最终锁定（TBD）。

### 3.2 FastAPI Control Plane

建议模块边界：

```text
app/
  api/
  auth/
  organizations/
  projects/
  memberships/
  connections/
  metadata/
  secrets/
  pipeline/
  artifacts/
  agent/
  policy/
  preparation/
  approvals/
  capability/
  broker/
  execution/
  scheduling/
  quality/
  supervision/
  rollback/
  evidence/
  benchmark/
  observability/
  shared/

```

路由层只负责输入校验和调用应用服务，不在 Router 中直接实现业务流程或执行外部 SQL/SeaTunnel。

### 3.3 Celery / Outbox

* Outbox Dispatcher 从 PostgreSQL 扫描可投递事件，按租约/锁策略领取并投递 Celery。
* Worker 消费命令，使用 `execution_id` / `command_id` 实现幂等。
* Worker 不直接相信 Celery Payload；重新读取执行事实和权限绑定信息。
* 任务重投不等于重新消费 Capability；Capability 消费与 ExecutionRun 创建在 Commit 的持久事务中完成。
* Worker 对外部 SeaTunnel 提交结果不确定时执行对账；无法确认时进入 `UNKNOWN`，不得盲目重发。

---

## 4. 身份、多租户和数据隔离

### 4.1 领域关系

建议实体：

* `User`
* `Organization`
* `OrganizationMember`
* `Project`
* `ProjectMember`
* `Role`
* `Permission`
* `RolePermission`
* `MemberRoleBinding`
* `PolicyRule`
* `Session`
* `RefreshTokenFamily`
* `EmailOtpChallenge`

一个 User 可加入多个 Organization。Project 属于一个 Organization。用户获得 Project 权限需要组织成员关系和项目成员关系共同满足；管理员和服务身份的特殊路径必须显式记录。

### 4.2 RBAC + ABAC

授权输入至少包含：

`subject, organization_id, project_id, action, resource, environment, data_classification, risk_level, artifact_hash, approval_context`

PDP 输出：

`decision, risk_level, required_approval_slots, constraints, reason_codes, policy_version`

自定义 Role 不得修改系统职责分离约束，不允许通过添加 Permission 绕过 Maker 自批限制。

### 4.3 PostgreSQL RLS

采用应用层授权与 PostgreSQL RLS 双重隔离。

**PROPOSED 安全要求：**

* Migration Role 与 Runtime Role 分离。
* Runtime Role 不拥有受 RLS 保护的表，且无 `BYPASSRLS`。
* 对关键表考虑 `FORCE ROW LEVEL SECURITY`。
* 每个请求/任务在事务范围内设置可信 Organization / Project 上下文。
* 使用事务级上下文，不依赖连接会话中遗留的租户变量。
* 后台 Worker 通过已验证的 ExecutionRun 派生租户上下文，而不是信任消息体中的 tenant_id。
* 对缺失上下文、跨项目 ID 猜测、连接池复用和后台任务路径进行负向测试。
* 对需要跨租户的系统级运维功能使用独立、受限、审计化的服务身份。

具体 RLS policy SQL 和连接池实现属于设计细化项，但必须在编码前确定。

---

## 5. 连接器、Metadata Profile 与 Secret

### 5.1 Connector Registry

建立版本化 Connector Registry，而不是在前端硬编码“支持”列表。

每个 Connector Capability 记录：

* Connector ID / 名称 / 版本。
* Source / Sink / Transform 能力。
* 支持的 SeaTunnel Engine 形态与版本。
* 认证方式。
* 元数据探查能力。
* Full Load / Incremental / CDC 能力。
* Delete Event / Upsert 能力。
* Checkpoint / Restore 能力。
* Schema Evolution 能力。
* 发布与回滚支持限制。
* 测试状态和证据链接。

状态至少区分 `PLANNED`、`CONFIGURED`、`INTEGRATION_TESTED`、`END_TO_END_VERIFIED`、`UNSUPPORTED`。不得把配置文件模板存在误标为真实支持。

### 5.2 Metadata Snapshot

建议实体：

* `MetadataProfile`
* `MetadataSnapshot`
* `MetadataSchemaObject`
* `MetadataColumn`
* `MetadataStatistic`
* `SensitiveClassification`
* `SchemaFingerprint`
* `SchemaDriftEvent`

Snapshot 不可被刷新操作覆盖。刷新创建新 Snapshot，保存采集时间、连接版本、Connector 版本、Schema Fingerprint、统计来源和分类规则版本。

敏感识别需支持自动检测与人工标记；脱敏规则、样本量和保留期限为 TBD。

### 5.3 SecretProvider

Provider 抽象至少提供：

* `put_secret`
* `get_secret_for_execution`
* `rotate_secret`
* `revoke_secret`
* `get_secret_metadata`

目标实现：Vault KV v2 Provider + 加密存储 Provider。开发环境与持久化运行方式都需要明确初始化、密钥备份、轮转和恢复策略。

约束：Secret 明文只在执行所需的最短生命周期内物化；不进入 EtlPlan、HOCON、Prompt、LLM 输入、Celery 普通日志、ExecutionRun 公开字段或 Evidence Ledger。Manifest 仅绑定受控 Secret Reference / Version，不保存值。

### 5.4 外部 LLM 数据策略

默认仅将执行 Agent 推理所需的表/字段 Schema 和脱敏样本发送给 OpenAI-compatible Provider。扩大数据范围需要项目策略授权、脱敏处理、调用审计和数据最小化。数据库密码、Token、连接 URI 凭据、原始敏感样本不得直接进入模型上下文。

Provider API Key 本身也是 Secret，不允许存入项目配置明文。

### 5.5 文件访问

支持：浏览器上传、受控挂载目录、S3/MinIO。

* 上传文件进入隔离对象存储区域，记录校验和、媒体类型、大小和归属。
* 本地路径只允许预配置根目录下的规范化路径；阻止 `..`、符号链接逃逸、绝对路径越界和容器宿主机敏感目录访问。
* MinIO Bucket / Prefix 与 Organization / Project 绑定。
* 文件扫描、压缩炸弹、超大文件和保留期限策略为 TBD。

---

## 6. Pipeline Artifact、DSL 与版本化

### 6.1 制品结构

采用模块化、版本化 DSL。PipelineVersion 通过 Artifact Manifest 绑定所有执行依赖。

```text
Pipeline
  └─ PipelineVersion (immutable)
       └─ ArtifactManifest
            ├─ EtlPlan DSL
            ├─ Source / Sink Profile References
            ├─ Schema Snapshot References
            ├─ Field Mapping
            ├─ Transform SQL
            ├─ SeaTunnel HOCON
            ├─ QualityContract
            ├─ RuntimeSupervisionContract
            ├─ Connector Capability Manifest
            ├─ Generator / Prompt / Model Metadata
            ├─ Compiler / Schema Version
            └─ Execution Dependency Manifest

```

Secret 只保留受控引用与版本。不能把模型隐藏推理链作为审计依赖；记录结构化输入、结构化输出、模型和 Prompt 版本、校验结果及必要调用元数据即可。

### 6.2 Pipeline Version

版本采用递增 Revision + SHA256 内容摘要。

示例：

```text
pipeline_id: pipe_...
revision: 12
artifact_hash: sha256:...
parent_revision: 11
created_by: user_...
created_at: ...

```

### 6.3 Canonicalization 与摘要

摘要对象为规范化 Artifact Manifest。必须定义：

* UTF-8 编码。
* Object Key 排序。
* 数值表示和精度。
* 字符串转义。
* 空值与缺失字段语义。
* Schema Version。
* 二进制对象使用内容摘要引用，不将非规范化文件路径纳入摘要。

若 Manifest 包含动态时间戳，需将其区分为制品元数据与内容字段，避免非语义性变化改变内容哈希。最终 canonical JSON 算法与测试向量为 TBD。

### 6.4 Preparation Fingerprint

建议至少包含：

* Pipeline Artifact Hash。
* Source / Target Metadata Snapshot Fingerprint。
* Connection Configuration Version。
* QualityContract Hash。
* RuntimeSupervisionContract Hash。
* Connector Capability Version。
* Execution Environment。
* Policy Version。
* Secret Reference / Version（不含 Secret 值）。
* Approval Requirement Digest。

变化处置按确定性分类器，不由 LLM 决定。重大变化使 Preparation 失效；可接受变化按策略重新评估并留下原因。最终变化分类矩阵 TBD。

### 6.5 PublishedDatasetVersion

`PipelineVersion` 表示逻辑执行制品；`PublishedDatasetVersion` 表示目标系统实际发布的数据状态。二者不能混为一谈。

发布记录至少保存：目标资源、执行 ID、源制品哈希、影子表标识、原表/旧版本引用、发布时间、质量报告摘要、Swap 结果、回滚状态和数据保留元数据。

---

## 7. LangGraph 编排

### 7.1 Graph 结构

CONFIRMED：一个主 StateGraph + 多个子 Graph；PostgreSQL Checkpointer。

建议主 Graph：

```text
START
  -> ParseIntentSubgraph
  -> ResolveProjectContext
  -> MetadataInspectionSubgraph
  -> ClarificationInterrupt? -----> WAIT_FOR_USER
  -> BuildEtlPlanSubgraph
  -> SQLAnalysisAndStrategyCompile
  -> HoconGenerationSubgraph
  -> DeterministicGateSubgraph
  -> RepairLoop (bounded)
  -> CandidateReview
  -> END

```

Prepare / Approve / Commit 由 FastAPI/Harness 控制面服务负责，不应把 Commit 作为 Agent Graph 中可直接执行的节点。Graph 可以生成 Prepare 请求草案，但实际 Prepare 必须走 API 与 PDP；审批由独立人类主体完成。

### 7.2 子 Graph 边界

* Intent Parser：解析 Source、Target、Mode、业务目标、约束和缺失信息。
* Metadata Inspector：只读探查、Schema Snapshot 引用、敏感分类结果。
* Plan Generator：生成结构化 EtlPlan DSL。
* SQL Analyzer / Compiler：解析、AST 安全检查、能力分析、执行位置选择。
* HOCON Generator：按 Connector Registry 生成配置，不包含 Secret 明文。
* Deterministic Gate：Schema、SQL、HOCON、Connector 和 Runtime Contract 校验。
* Repair Subgraph：只修改失败候选，不可自行放宽策略；修复次数上限 TBD。

### 7.3 Checkpoint 与中断

* `thread_id` 关联一次 Studio 设计会话。
* Checkpoint 保存在 PostgreSQL。
* 恢复时重新验证 user、organization、project、thread 的访问权限。
* Conversation State、Workflow State 与 Execution State 分开存储。
* Graph 中断用于缺失参数澄清和必要人工输入，不将审批/Commit 暴露成 Agent 可绕过的 Graph 节点。

### 7.4 Provider

第一阶段实现 OpenAI-compatible Provider，支持配置 Base URL、Model、API Key Reference、Timeout、Retry 和结构化输出模式。真实 Provider 调用失败时不能默默将不完整结果作为合法制品；Benchmark 提供 Deterministic Mock Provider 以保证可重复测试。

---

## 8. Harness 安全内核

### 8.1 PDP 风险等级

CONFIRMED 策略映射：

| 等级 | 处置 |
| --- | --- |
| P0 | 拒绝，不允许通过审批放行 |
| P1 | 双独立审批 |
| P2 | 单独审批 |
| P3 | 简化审批/策略允许范围内自动放行 |

具体风险规则、环境差异、数据分类映射和审批角色槽位的适用细节需要策略版本化，不能由 Agent 临时决定。

### 8.2 Prepare / Approve / Commit

**Prepare**

* 无外部副作用。
* 计算输入指纹、资源范围、数据分类、预算、回滚计划、审批槽位。
* 冻结 Preparation 事实。
* 保存策略版本、Connector Capability 版本和依赖快照。

**Approve**

* 审批人对冻结事实做具名决定。
* Data Checker 与 Security Checker 分别完成职责。
* Maker 不可自批。
* 审批记录绑定 Preparation ID、Fingerprint、审批槽位和内容摘要。
* Preparation 事实变化后按策略失效或重新评估。

**Commit**

* Operator 发起。
* 服务端重新校验 Preparation、Pipeline Artifact Hash、Metadata Fingerprint、Approval Digest、Policy Version、预算和资源权限。
* Capability 由签名服务签发。
* 单次消费事实、ExecutionRun 和 Outbox 命令在明确的 PostgreSQL 事务中原子创建。
* 不允许前端直接调用 SeaTunnel API。

### 8.3 Capability Token

目标：Ed25519 签名的 JWT/JWS，TTL 5 分钟。

建议 Claims：

```text
iss
sub
aud
jti
iat
exp
organization_id
project_id
environment
tool
preparation_id
pipeline_version_hash
approval_digest
policy_version
nonce

```

Token 绑定主体、工具、环境、Project、Preparation、制品哈希和审批摘要。实现必须验证标准 JWT/JWS EdDSA 兼容性，尤其是 Vault Transit 返回值到 JWS signature bytes 的编码适配。

### 8.4 单次消费与重放防护

CONFIRMED：JWT + PostgreSQL 唯一消费记录 + Redis 快速防重放。

关键语义：

* PostgreSQL 唯一约束是最终单次消费事实。
* Redis 仅作快速拒绝/缓存层，不能是唯一事实来源。
* Capability 消费、ExecutionRun 创建和 Outbox 命令必须有一致的事务设计。
* Celery At-least-once 重投不应再次消耗或重新执行同一 Capability。
* Worker 对已提交命令做签名验证、授权绑定验证和幂等检查；重复 `command_id` / `execution_id` 不能生成第二个外部副作用。

**PROPOSED 事务边界：** Tool Broker 在 Commit 时验证并记录 `jti` 消费，创建 ExecutionRun 和 Outbox。Worker 后续验证已持久化的授权命令与 Token 内容，但不把同一 Outbox 重投当作第二次 Capability 消费。

### 8.5 Transactional Outbox

CONFIRMED：Outbox Dispatcher + Celery，预留 CDC。

建议实体：

* `OutboxEvent(id, aggregate_type, aggregate_id, event_type, payload_ref, created_at)`
* `OutboxDelivery(id, outbox_event_id, status, attempts, lease_until, delivered_at, last_error)`

Dispatcher 使用数据库事务锁/租约领取事件；投递成功后标记 Delivered。崩溃或重复投递允许消息重发。Worker 必须以稳定 command ID 幂等处理。具体锁实现和 Dispatcher 并发模型 TBD。

### 8.6 Evidence Ledger

CONFIRMED：SHA256 哈希链 + 周期性 Ed25519 签名检查点。

事件至少包含：

`event_id, timestamp, actor, organization_id, project_id, event_type, resource_ref, artifact_hash, payload_digest, previous_hash, event_hash`

* 使用 canonical serialization。
* `event_hash = SHA256(canonical_event_without_hash + previous_hash)`，最终字节布局需锁定。
* 每 N 个事件或固定周期生成 Checkpoint，由 Ed25519 签名。
* 事件追加，不提供业务 API 修改历史事件。
* 定期执行链校验并报警。
* 数据库管理员仍可能重写数据库整体状态；L2 是可检测篡改的证据机制，不是外部 WORM 存储。
* 检查点导出/外部锚定是否实现为 P1/P2 TBD。

---

## 9. SQL 编译与安全沙箱

### 9.1 两类 SQL

* SQL Transformation：业务 ETL 转换 SQL。
* SQL Quality Rule：数据质量查询/规则 SQL。

SQL 仅允许 SELECT。复杂 SELECT 的具体语法与两个类别是否采用相同白名单为 TBD。

### 9.2 安全流水线

```text
SQL Text
  -> Dialect Parser
  -> AST Validation
  -> Statement Type Gate (SELECT only)
  -> Table / Column Allowlist
  -> Function Allowlist
  -> Join / Subquery / CTE Policy
  -> Query Cost / Timeout Budget
  -> Read-only DB Identity
  -> Execution Strategy Compiler
  -> Explain / Dry-run (where supported)
  -> Controlled Execution

```

禁止只用正则或字符串搜索判断 SQL 安全。Parser 通过不代表 SQL 安全；执行必须使用权限受限的只读身份，并限制 Schema、表、列、函数、行数、字节数、运行时间和并发。

危险函数、系统目录访问、跨库访问、外部函数、文件读写、用户定义函数和方言扩展的允许范围均为 TBD。

### 9.3 Execution Strategy Compiler

Compiler 根据确定性能力矩阵选择：

* Source Pushdown。
* SeaTunnel Transform。
* Target-side SQL。

决策输入包括 SQL 方言兼容性、Source/Sink 能力、数据量估计、网络/资源预算、Connector 功能和安全约束。输出必须记录选定策略与原因；LLM 不直接指定最终执行位置。

---

## 10. SeaTunnel 数据面

### 10.1 Zeta 集群

CONFIRMED：长期运行的 SeaTunnel Zeta 集群；默认单节点，保留多节点扩展。

FastAPI/Worker 通过 `SeaTunnelAdapter` 提交和查询作业。Zeta REST API 应仅在 Docker 内部网络暴露。REST API 与集群内部 Hazelcast 通信端口分开管理；端口、认证、TLS 与部署配置按锁定版本确认。

### 10.2 Connector 能力矩阵

每个 Connector / Engine / Sink 组合必须通过集成测试确认：

* 真实连接。
* Metadata Probe。
* 全量读取和写入。
* 增量边界与水位线。
* CDC 初始快照。
* Update / Delete 事件。
* Checkpoint 与重启恢复。
* Schema 变化。
* Sink Upsert 语义。
* 质量分流兼容性。
* 影子表发布与回滚。

初始目标：MySQL → Doris 批处理和 CDC；PostgreSQL → Doris 的 CDC 单独验证。Connector 版本、参数和可用能力为 VERSION-CHECK。

### 10.3 提交与未知状态对账

SeaTunnel 提交是外部副作用。Worker 需要先持久化 `SUBMITTING` 意图和稳定外部作业标识，再提交。提交响应丢失或 Worker 崩溃时，系统使用稳定作业标识向 Zeta 查询并对账。若无法确定 Job 是否已创建，ExecutionRun 进入 `UNKNOWN`，等待受控恢复，不盲目重新提交。

SeaTunnel API 是否提供足够稳定的幂等提交能力，需按目标版本验证；未验证前不能宣称提交 Exactly-once。

### 10.4 影子表、错误表与发布

* 数据有效路径写入影子表或暂存资源。
* 违反 QualityContract 的数据写入错误表，记录 ErrorCode、Rule ID 和原因。
* 质量门禁与运行预算通过后进入发布阶段。
* Doris OLAP 表使用 `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')` 时，可保留旧表数据用于恢复。
* Doris 替换只适用于 OLAP 表；Doris 不检查两张表 Schema 是否一致，因此平台必须先做结构兼容校验。
* 其他目标数据库通过独立 `PublishAdapter` 定义能力，不假设所有 DDL 都是事务原子操作。

### 10.5 CDC

CDC 生命周期至少处理：

* Initial Snapshot。
* Source Log / Binlog / WAL Position。
* Replication Slot（适用于 PostgreSQL）。
* Checkpoint。
* Duplicate Event。
* Update / Delete Event。
* Sink Upsert。
* Schema Evolution。
* Restart / Pause / Stop。
* Lag 与 WAL/Binlog 保留。
* 位点失效与重新快照。

用户选择 C + D：先尝试恢复可用历史检查点；无法恢复时检测风险，并要求 Operator 确认重新快照/重新建 Slot 等恢复动作。不能静默丢弃位点或自动回退到无告警的全量重跑。

### 10.6 一致性声明

* Outbox：At-least-once。
* Celery：消息允许重复投递。
* Control Plane：ExecutionRun / command 幂等。
* Batch Sink：尽量使用影子表、唯一键或 Upsert 降低重复影响。
* CDC：依据 Connector 与 Sink 的实际语义声明交付保证。
* 端到端 Exactly-once 不作为默认平台承诺。

---

## 11. ExecutionRun 状态与监督

### 11.1 统一主实体、分模式状态机

CONFIRMED：Batch 与 CDC 共用基础 ExecutionRun 记录，但生命周期不同。

建议字段：

* `execution_id`
* `organization_id`, `project_id`
* `pipeline_id`, `pipeline_version_id`, `artifact_hash`
* `execution_mode` (`BATCH` / `CDC`)
* `preparation_id`, `capability_jti`, `command_id`
* `external_engine`, `external_job_id`
* `status`, `status_reason`
* `started_at`, `ended_at`
* `source_position`, `checkpoint_ref`（CDC）
* `metrics_snapshot_ref`
* `published_dataset_version_id`

### 11.2 批处理生命周期

候选状态（精确枚举与迁移守卫 TBD）：

`CREATED → QUEUED → SUBMITTING → RUNNING → VALIDATING → PUBLISHING → SUCCEEDED`

异常/控制状态：

`PAUSE_REQUESTED, PAUSED, CANCEL_REQUESTED, CANCELLED, INTERRUPTED, FAILED, UNKNOWN, RECONCILING, ROLLING_BACK, ROLLED_BACK`

暂停是否可用取决于执行引擎和 Connector；若不支持安全暂停，产品应显示受管停止/重启，而不是模拟暂停。

### 11.3 CDC 生命周期

CDC 是持续作业，不以普通批处理的 `SUCCEEDED` 作为长期运行终态。候选状态：

`CREATED → INITIAL_SNAPSHOT → STREAMING → DEGRADED / PAUSE_REQUESTED / STOP_REQUESTED → STOPPED`

异常状态可能包括 `FAILED`、`CHECKPOINT_LOST`、`POSITION_EXPIRED`、`UNKNOWN`、`RECOVERING`。具体状态迁移与可恢复条件按 Connector 能力矩阵确定。

### 11.4 RuntimeSupervisionContract

字段候选：

* `max_read_rows`
* `max_write_bytes`
* `max_duration_seconds`
* `max_amplification_ratio`
* `max_reject_rate_total`（用户确认阈值 20%）
* `max_reject_rate_window`
* `window_definition`
* `min_validated_rows`
* `max_error_rows_absolute`
* `action_on_breach`

Reject Rate：

`error_rows / validated_rows`

触发条件：整体率、窗口率、绝对错误行数和其他预算按 Contract 组合判断。最小样本量、窗口定义、绝对上限和默认动作 TBD。

### 11.5 诊断

诊断引擎可分析日志、异常类型、Connector 错误、Schema Drift、ErrorCode 和近期运行指标，生成根因假设与建议。诊断结果必须标记为建议/置信度，不得自动执行写操作或回滚。

---

## 12. Schedule 与 Celery Beat

### 12.1 Schedule 实体

Schedule 独立于 PipelineVersion 保存，建议字段：

`id, pipeline_id, pipeline_version_binding, cron_expression, timezone, enabled, concurrency_policy, misfire_policy, next_run_at, last_triggered_at`

### 12.2 已确认能力

* 即时执行。
* Cron 定时执行。
* 时区、夏令时、错过调度处理。
* 并发锁与积压管理。
* Schedule 可独立启停。

### 12.3 TBD

* 默认错过调度策略（Skip / Fire Once / Catch Up）。
* 默认并发策略（FORBID / ALLOW / REPLACE）。
* Schedule 绑定固定 PipelineVersion 还是使用最新版本。
* Cron parser 和时区数据库的版本锁定。

不得在没有明确产品决策时静默实现某一默认行为。

---

## 13. 发布、版本保留与回滚

### 13.1 PublishedDatasetVersion

每次发布成功创建记录，引用：

* 目标表/资源。
* 关联 ExecutionRun。
* PipelineVersion / Artifact Hash。
* 发布前数据版本。
* 发布后数据版本。
* 影子表、Swap 结果和旧数据资源引用。
* Quality Report。
* 发布时刻与操作者。

### 13.2 回滚类型

1. **未发布影子数据清理**：删除受管影子数据和临时资源。
2. **发布后版本恢复**：根据 PublishedDatasetVersion 和目标发布能力恢复前一数据状态。
3. **CDC 补偿/恢复**：处理消费位点、检查点、Sink 幂等和重放范围；需要额外审批。

回滚不是删除历史记录；所有回滚均创建新审计事件和执行记录。

### 13.3 保留策略

PipelineVersion、Preparation、Approval、Evidence Ledger 和 Benchmark Results 长期保留；ExecutionRun、日志、PublishedDatasetVersion、Metadata Sample 按策略保留。具体期限、存储上限、归档和清理授权 TBD。

---

## 14. API 契约边界

最终 URL、HTTP Method、请求/响应 JSON Schema 和错误码需在实现前由 OpenAPI 契约生成并冻结；以下为模块级契约范围，不是已确认的路径清单。

| 模块 | API 能力 |
| --- | --- |
| Auth | 登录、刷新、登出、Email OTP challenge/verify、会话管理 |
| Organizations | 组织 CRUD、成员邀请/移除、组织角色 |
| Projects | 项目 CRUD、成员、角色、策略 |
| Connections | 创建/更新/禁用连接、连通性测试、Secret 引用 |
| Metadata | Probe、Snapshot、Refresh、Drift 查询 |
| Agent | 创建 Thread、提交需求、Resume、Graph State 查询 |
| Pipeline | 草稿、候选、校验、版本列表、Artifact 下载/摘要 |
| Preparation | Prepare、查看冻结事实、失效查询 |
| Approvals | 审批槽位、Approve/Reject、审批历史 |
| Commit | Operator Commit、提交状态查询 |
| Execution | Run 查询、指标、日志、取消/停止/恢复请求 |
| Scheduling | Schedule CRUD、启停、触发记录 |
| Quality | Contract CRUD、报告、错误数据摘要 |
| Rollback | 回滚 Prepare、审批、Commit、状态查询 |
| Evidence | Ledger 查询、校验、Checkpoint 查询 |
| Benchmark | Dataset、Run、指标、模型/策略对比 |

所有 API 必须：

* 校验 Organization / Project 上下文。
* 对受保护资源执行 RBAC + ABAC。
* 返回稳定错误码和 Trace ID。
* 对幂等操作定义 Idempotency-Key 或等效机制。
* 对敏感字段进行响应过滤。
* 对长任务返回资源 ID，而不是同步等待长时间数据执行。

---

## 15. 数据模型清单（初始领域模型）

最终 ERD 与完整字段约束待生成/评审。初始实体分组：

### 15.1 IAM / Tenant

`users, organizations, organization_members, projects, project_members, roles, permissions, role_permissions, member_role_bindings, policy_rules, sessions, refresh_token_families, email_otp_challenges`

### 15.2 Connections / Metadata / Secrets

`data_connections, connection_versions, secret_references, metadata_profiles, metadata_snapshots, metadata_objects, metadata_columns, metadata_statistics, sensitive_classifications, schema_fingerprints, schema_drift_events, uploaded_files, object_storage_refs`

### 15.3 Pipeline / Agent / Artifacts

`agent_threads, workflow_runs, pipeline_definitions, pipeline_drafts, pipeline_versions, artifact_manifests, artifact_blobs, etl_plan_modules, sql_artifacts, hocon_artifacts, quality_contracts, runtime_contracts, connector_capability_versions`

### 15.4 Governance / Harness

`preparations, preparation_fingerprints, approval_slots, approval_decisions, capability_issuances, capability_consumptions, execution_commands, replay_records, outbox_events, outbox_deliveries`

### 15.5 Execution / Quality / Publish

`execution_runs, execution_state_events, external_job_refs, execution_metrics, execution_logs_refs, quality_reports, quality_rule_results, rejected_record_summaries, published_dataset_versions, rollback_runs, schedules, schedule_triggers`

### 15.6 Evidence / Benchmark

`evidence_events, evidence_checkpoints, evidence_verification_runs, benchmark_datasets, benchmark_cases, benchmark_runs, benchmark_case_results, model_provider_versions, policy_versions, generator_versions`

必须设置适当的唯一约束、外键、租户索引、时间索引、状态索引、JSON Schema 版本和归档策略。具体列、分区、保留策略和数据库迁移设计仍需细化。

---

## 16. 可观测性与审计

### 16.1 可观测性栈

* Prometheus：应用和基础设施指标。
* Grafana：统一 Dashboard。
* Loki：应用/Worker/执行日志。
* OpenTelemetry SDK / Collector：统一 Trace / Metrics / Logs 采集入口。
* Tempo：Trace 存储与查询。

### 16.2 Trace Context

建议使用以下关联标识：

`trace_id, organization_id, project_id, pipeline_id, pipeline_version_id, preparation_id, execution_id, external_job_id`

日志中不得记录凭据、完整连接 URI、原始敏感行或 Capability 私密材料。

### 16.3 审计与运行日志分离

* Application Logs：服务运行诊断。
* Metrics：数值时序。
* Traces：跨服务调用链。
* Evidence Ledger：安全与业务关键事实的哈希链/签名证据。

Evidence Ledger 不能由普通日志替代；Logs 也不能被用作唯一审计记录。

---

## 17. Benchmark 与测试策略

### 17.1 分层

* L0：静态注入（Prompt Injection、非法 SQL、超范围 ToolIntent、敏感字段外发、重放样例）。
* L1：模拟故障（数据库断连、延迟、Worker Crash、重复投递、Zeta 提交响应丢失、拒绝率超限、Schema Drift）。
* L2：真实链路（实际数据库、SeaTunnel、Vault、审批、Capability、Outbox、Doris 发布与恢复）。
* Regression：每次 Generator / Model / Policy / Connector 版本变更后回归。

### 17.2 原始指标

`plan_validity, hocon_validity, schema_mapping_accuracy, sql_safety_detection, p0_block_rate, p1_block_rate, replay_prevention, repair_success_rate, end_to_end_success, recovery_success, connector_capability_pass_rate`

综合评分按数据生成、安全治理、执行可靠性三个维度组织。权重 TBD。P0/P1 安全硬门槛独立判定，不能被综合评分抵消。

### 17.3 自动化测试类别

* Unit：领域规则、Canonicalization、PDP、AST Gate、状态迁移。
* Contract：OpenAPI、EtlPlan JSON Schema、Connector Registry、Outbox Payload。
* Integration：PostgreSQL RLS、Vault、Redis/Celery、Zeta、Doris。
* E2E：两条核心业务场景。
* Fault Injection：重复投递、Worker 终止、提交响应丢失、CDC 位点丢失。
* Security：跨租户访问、Maker 自批、Capability 重放、SQL 越权、Secret 泄露、文件路径逃逸。

### 17.4 数据规模

10K / 100K / 1M 三档，固定随机种子，可生成格式异常、空值、超范围、重复主键和 Schema Drift 数据。

---

## 18. 多 Agent Vibe Coding 工作约定

### 18.1 角色

* Lead / Architect Agent：架构决策、契约审核、集成排序。
* Backend Agent：FastAPI、IAM、数据库模型、API。
* Agent Workflow Agent：LangGraph、Provider、EtlPlan、Gate。
* Frontend Agent：Vue 控制台、Studio、DAG、Diff。
* Data Agent：SeaTunnel、Connector、Doris、CDC、Quality。
* Security Agent：PDP、Capability、Replay Guard、RLS、Vault、Evidence。
* QA Agent：Unit、Contract、Integration、E2E、Benchmark。

### 18.2 协作规则

CONFIRMED：独立 Git Worktree + 分阶段协调。

* 每个 Agent 有明确分支、Worktree、文件所有权和输出契约。
* 先冻结共享 API / DSL / DB Migration 契约，再并行开发。
* 不允许两个 Agent 同时修改同一个核心 Schema 或共享迁移文件；需要通过 Lead 合并。
* 每个模块交付前执行自身单测和契约测试。
* Security Agent 在 IAM、Secret、SQL、Tool Broker、Commit、Worker、Rollback 模块交付时参与，不只做末尾审计。
* QA Agent 维护可重复的最小集成环境和回归集合。
* Agent 不能因测试困难而删除测试、关闭安全 Gate 或放宽权限。
* 关键决策必须形成 ADR；未确定问题使用 TBD，不允许 Agent 自行补完。

### 18.3 推荐实施顺序（PROPOSED，不是已确认排期）

1. Repo、Compose（含 18GB 内存限额配置）、基础工程、CI 和可观测性骨架。
2. IAM、Organization/Project、RBAC/ABAC、RLS。
3. Connection、SecretProvider、Metadata Snapshot。
4. Pipeline DSL、Artifact Manifest、Schema Gate。
5. LangGraph 需求澄清/生成/Checkpoint。
6. Harness Prepare/Approve/Commit、Capability、Outbox、Evidence。
7. Zeta Adapter、MySQL → Doris 批处理真实链路。
8. QualityContract、Runtime Supervision、Doris Publish/Swap。
9. CDC 初始链路和恢复测试。
10. Studio / Approval / Execution UI 完整闭环。
11. Benchmark、Grafana 大盘、故障演示。
12. 安全回归和文档验收。

各阶段具体 P0/P1/P2 分类、完成标准和工期未由用户确认，不在本规格中伪装为已批准计划。

---

## 19. 风险登记

| ID | 风险 | 处理要求 |
| --- | --- | --- |
| R-01 | 单 Compose 默认启动所有服务可能超出 **18GB** 限额 | 冷启动、稳态和压测；严格限制 JVM 堆内存，不通过则重新评审资源分配 |
| R-02 | Windows 宿主 + Antigravity GUI + WSL2 并发高负载时内存争用 | 配置 `.wslconfig` 锁定 WSL2 上限（20GB），预留 12GB 给 Windows 宿主与 IDE |
| R-03 | Email OTP 安全强度不足 | 标注为邮件二次验证；TOTP/WebAuthn 升级 TBD |
| R-04 | SELECT 仍可能越权或资源耗尽 | AST + 对象白名单 + 只读账号 + 执行预算 |
| R-05 | CDC Connector 行为随版本变化 | 版本化能力矩阵 + L2 真实测试 |
| R-06 | SeaTunnel 提交响应丢失可能产生重复 Job | 稳定 Job 标识、查询对账、UNKNOWN 状态，不盲目重试 |
| R-07 | PostgreSQL RLS 被高权限账号绕过 | Migration/Runtime Role 分离和 RLS 负向测试 |
| R-08 | Doris Swap 不验证 Schema 兼容 | 平台发布前执行 Schema 兼容 Gate |
| R-09 | 哈希链可能被具备数据库控制权的主体整体重写 | Ed25519 Checkpoint；外部 WORM 锚定未包含在 L2 |
| R-10 | LLM 输出幻觉/Prompt Injection | 工具最小权限、确定性 Gate、受管上下文和审批 |
| R-11 | Redis/ Celery 重复消息造成副作用重复 | PostgreSQL 幂等事实、command ID 和 Worker 对账 |
| R-12 | 数据/日志/样本增长超过 200GB | 保留策略、磁盘监控和清理审核 |

---

## 20. 未决事项清单（TBD）

### 20.1 产品与安全策略

* Email OTP 是否仅用于演示二次验证，还是必须升级为 TOTP / WebAuthn。
* 自定义 SQL 的方言、AST 节点、函数白名单、跨 Schema 限制及 Query Cost 策略。
* P0/P1/P2/P3 的具体策略规则、每个审批槽位分配和 ABAC 属性矩阵。
* Preparation 指纹变化分类表和重新审批门槛。
* 外发 LLM 数据的项目授权 UI、策略审计字段和拒绝行为。

### 20.2 数据与执行

* Oracle / ClickHouse / 文件 / REST API 的首批端到端验收顺序。
* 各 Connector 的固定版本、Checkpoint、Delete / Upsert、Schema Evolution 支持矩阵。
* CDC 初始快照、位点失效、Replication Slot/WAL 保留和补偿规则。
* 批处理与 CDC 的完整状态枚举、迁移图和终止语义。
* Reject Rate 最小样本量、窗口长度、绝对错误阈值和窗口计算方法。
* 质量规则失败数据的原始值保存和脱敏策略。
* SQL Transformation 与 Quality Rule 是否使用相同的 SELECT AST 白名单。

### 20.3 平台与部署

* 完整默认 Compose 服务清单及各服务在 **18GB** 总配额下的内存限额拆分（特别是 Doris FE/BE 与 SeaTunnel JVM）。
* 18GB Docker 内存下的全服务冷启动与 10K/100K/1M 压测结果。
* Vault 默认运行模式、初始化、Unseal、备份和恢复流程。
* SeaTunnel Zeta 多节点 Profile 的实际启动配置。
* Doris 单 FE / 单 BE 镜像与版本锁定。


### 20.4 API / 数据库 / UX

* OpenAPI URL、请求/响应 Schema、错误码枚举和分页标准。
* 完整 ERD、字段级类型、唯一索引、外键与分区策略。
* WebSocket / SSE 的最终选择。
* Schedule 的默认 misfire、concurrency 和 PipelineVersion 绑定策略。
* Metadata Sample、ExecutionRun、Logs、PublishedDatasetVersion、Evidence 与 Benchmark 的保留期限。
* Benchmark 权重和各指标门槛。
* 自动修复次数、模型重试次数、超时和 Token 预算。

---

## 21. 官方参考资料

* Google Antigravity Remote Agents & WSL： [https://antigravity.google/docs/remote-agents](https://antigravity.google/docs/remote-agents)
* Microsoft WSL Configuration (`.wslconfig`)： [https://learn.microsoft.com/en-us/windows/wsl/wsl-config](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
* Microsoft WSL Interop： [https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)
* Docker Desktop WSL 2 Backend： [https://docs.docker.com/desktop/features/wsl](https://docs.docker.com/desktop/features/wsl)
* Docker Desktop WSL Best Practices： [https://docs.docker.com/desktop/features/wsl/best-practices](https://docs.docker.com/desktop/features/wsl/best-practices)
* Apache SeaTunnel Remote Zeta Job Submission： [https://apache.googlesource.com/seatunnel/+/dcf97867ac13c04661a35af8568f2e3e40d07ef1/docs/en/getting-started/submit-job-to-remote-zeta-cluster.md](https://apache.googlesource.com/seatunnel/+/dcf97867ac13c04661a35af8568f2e3e40d07ef1/docs/en/getting-started/submit-job-to-remote-zeta-cluster.md)
* Apache Doris Atomic Table Replacement： [https://doris.apache.org/docs/4.x/data-operate/delete/atomicity-replace/](https://doris.apache.org/docs/4.x/data-operate/delete/atomicity-replace/)
* NIST SP 800-63B: [https://pages.nist.gov/800-63-4/sp800-63b.html](https://pages.nist.gov/800-63-4/sp800-63b.html)
