# 生产级 ETL-Agent 数据集成平台 — PRD

> 文档类型：产品需求规格说明书（PRD）  
> 版本：v1.0（五轮需求访谈基线）  
> 状态：评审基线，可用于拆分实施任务；未决项以 `TBD` 标识  
> 日期：2026-11-22  
> 产品形态：Vue Web 控制台 + FastAPI 控制面 + LangGraph Agent + Celery Worker + Apache SeaTunnel Zeta 数据面

---

## 1. 产品概述

### 1.1 产品定位

建设一个面向企业异构数据集成场景的 ETL-Agent 控制面参考实现。平台允许数据工程师通过自然语言描述数据集成目标，由 LangGraph 生成结构化、版本化的 ETL 制品；由确定性编译与门禁组件验证；再通过 Harness 安全内核执行 `prepare → approve → commit`；最终由 Celery Worker 调用 Apache SeaTunnel Zeta 完成受管数据搬运、质量分流、运行监督和受管发布。

本项目定位为**高完成度作品集项目 / 面向生产架构的参考实现**。目标是形成真实可运行的端到端工程系统，而不是宣称已经经过企业生产环境认证。涉及高可用、灾备、合规认证、跨集群容灾等能力，除非另有明确验收，不作为已交付承诺。

### 1.2 核心产品价值

1. 将自然语言 ETL 需求转化为可验证、可追踪、可版本化的执行制品。
2. 将 Agent 的规划能力与实际副作用执行隔离，禁止 Agent 直接写库或提交 SeaTunnel 作业。
3. 通过 Maker-Checker、PDP、单次 Capability、Replay Guard、Outbox 和 Evidence Ledger 形成受管执行闭环。
4. 通过 SeaTunnel 执行真实的数据读取、转换、写入、错误分流和发布。
5. 通过 QualityContract、RuntimeSupervisionContract 和 Benchmark 使数据质量与运行风险可量化、可解释、可回归。

### 1.3 主要演示场景

**场景 A：MySQL → Doris 订单用户宽表同步与发布**

数据工程师登记连接，探查订单表和用户表，输入自然语言需求。Agent 发现主键去重或增量字段等必要参数缺失时中断澄清；恢复后生成 EtlPlan、SQL Transformation 和 SeaTunnel HOCON。静态门禁通过后冻结 PipelineVersion，生成 Preparation。数据审批人与安全审批人分别审批，Operator Commit。SeaTunnel 将数据写入 Doris 影子表；质量规则将不合格记录分流；通过质量与运行预算检查后执行原子表替换，保留旧数据用于受管恢复。

**场景 B：日志清洗异常、拒绝率超限与受管回滚**

日志源格式发生变化，错误记录持续分流。运行监督同时检查整体拒绝率、滚动窗口拒绝率和错误绝对行数。超过配置阈值后触发硬中断，记录失败快照、错误码和可解释诊断。Operator 确认后清理未发布影子数据，或根据已发布数据版本恢复。CDC 场景还需处理检查点和位点恢复语义。

---

## 2. 项目边界与交付原则

### 2.1 已确认交付形态

- Vue 3 + TypeScript Web 控制台。
- FastAPI 控制面服务。
- LangGraph 主 StateGraph + 子 Graph。
- Celery Worker / Beat 与 Outbox Dispatcher。
- PostgreSQL、Redis、Vault、MinIO。
- Apache SeaTunnel Zeta 集群适配。
- Doris、MySQL、PostgreSQL 演示数据库。
- Prometheus、Grafana、Loki、OpenTelemetry、Tempo。
- 自动化测试、版本化 Benchmark 数据集和端到端演示链路。
- 交付两份核心 Markdown 文档：`PRD.md`、`TECHNICAL_SPEC.md`。

### 2.2 数据源范围

平台连接器目标范围：

- 数据库：MySQL、PostgreSQL、Oracle、Doris、ClickHouse。
- 文件：CSV、Excel、JSON、Parquet。
- 对象存储：S3、MinIO。
- API：REST API。

用户选择尽可能多地接入真实连接器，并以 SeaTunnel 官方成熟 Connector 的实际能力为准。**“连接器存在”不等于“所有同步模式均已支持”**。每种 Source/Sink 与同步模式必须通过版本化能力矩阵单独列出连接、元数据、全量、增量、CDC、删除事件、检查点、恢复和发布能力。

初始重点端到端链路为 MySQL → Doris；PostgreSQL CDC → Doris 纳入真实集成验证。Oracle、ClickHouse、文件及 REST API 的本地测试依赖和完整执行优先级仍需按能力矩阵确定。

### 2.3 同步模式

目标支持：

- 全量同步（Full Load）。
- 增量同步（例如更新时间或递增键；具体策略受 Source/Sink Connector 能力约束）。
- CDC（Change Data Capture）。
- 即时执行和 Cron 定时执行。

批处理与持续 CDC 使用不同执行生命周期，不强行统一为同一套暂停、完成和重试语义。

### 2.4 非目标与明确限制

- 不允许 Agent 直接执行数据库 DDL/DML、直接写 Sink、直接提交 SeaTunnel Job 或签发 Capability。
- 不将 LLM 输出视为可信配置；模型输出必须经过 Schema、AST、HOCON、资源范围和策略校验。
- 不承诺跨 PostgreSQL、Redis、Celery、SeaTunnel 和 Doris 的分布式 ACID 或通用 Exactly-once。
- 不将仅有哈希链的数据库记录宣称为外部 WORM 级不可删除存储。
- 不默认开放用户任意宿主机文件路径。
- 不将邮件 OTP 描述为抗钓鱼强 MFA。

---

## 3. 用户、组织与权限

### 3.1 组织模型

采用：

`User ↔ OrganizationMember ↔ Organization → Project ↔ ProjectMember`

一个用户可加入多个 Organization；一个 Organization 可包含多个 Project。Project 资源必须同时验证 Organization 成员资格和 Project 成员资格。数据库行级隔离和 API 授权均以组织/项目上下文为基础。

### 3.2 用户角色

| 角色 | 核心职责 | 禁止事项 |
|---|---|---|
| Maker（数据工程师） | 登记连接、元数据探查、需求澄清、生成候选、提交版本及 Prepare | 不得审批自己的申请；不得直接执行副作用 |
| Data Checker（数据审批人） | 审查字段映射、Schema、脱敏结构、质量规则和 DAG | 不得与同一高风险申请的其他审批槽位冲突 |
| Security Checker（安全审批人） | 审查数据分级、资源范围、Secret 引用、预算、回滚计划和策略 | 不得与同一高风险申请的其他审批槽位冲突 |
| Operator（系统操作员） | 在审批通过后 Commit、监控、暂停/取消、回滚 | 不得绕过审批或自行签发 Capability |
| Auditor（审计人员） | 查看执行历史、证据账本、质量报告和 Benchmark | 默认只读 |
| Organization / Project Admin | 管理成员、固定系统角色、自定义角色和项目策略 | 不得修改不可绕过的职责分离硬约束 |

固定系统角色不可删除或被自定义角色覆盖。允许 Project 管理员创建自定义角色，但职责分离、PDP 强制规则和受管执行约束始终由系统策略控制。

### 3.3 权限模型

使用 RBAC + ABAC：

- RBAC：确定用户具备的功能权限。
- ABAC：结合 Organization、Project、资源范围、环境、数据分级、风险等级、制品指纹和审批上下文决定是否允许操作。
- 高风险审批按 PDP 要求动态分配独立职责槽。
- 申请人禁止自批。
- 同一高风险申请中的 Maker、Data Checker、Security Checker 必须满足职责分离；Operator 的重叠约束由 PDP 策略确定。

### 3.4 认证

已选择 JWT Access Token + Refresh Token + MFA，MFA 选择 Email OTP。邮件验证码是本项目当前选择，但不能等同于抗钓鱼强认证；NIST SP 800-63B 明确不允许使用 Email 作为带外认证通道。建议将其在界面和文档中称为“邮件二次验证”，并将 TOTP / WebAuthn 列为后续增强项，是否调整为真正 MFA 仍为 `TBD`。

---

## 4. 产品信息架构与页面需求

### 4.1 总览工作台

展示：

- 当前 Organization / Project / Environment。
- 数据源连接数量及健康状态。
- Pipeline 数量、最近版本和最近执行状态。
- 待 Prepare、待审批和待 Operator Commit 的门禁概况。
- 批处理与 CDC 运行数量。
- 运行成功率、拒绝率、资源预算使用情况。
- 核心资源与最近审计事件。
- 快捷入口：连接管理、Pipeline Studio、审批中心、运行中心、Benchmark。

### 4.2 数据源与资产管理

功能：

1. 新建、编辑、禁用和测试连接。
2. 支持 SecretProvider 抽象；凭据只显示掩码，不在 API 响应、日志或前端状态中回显明文。
3. 受管只读 Metadata Probe：Schema、表、列、类型、主键、索引、近似统计。
4. 敏感字段识别和人工标记。
5. 脱敏样本展示；采样数量和样本保存期限受策略控制。
6. Metadata Snapshot 版本化、手动/定时刷新、Schema Fingerprint、Drift 历史对比。
7. 文件上传、受控挂载目录和 S3/MinIO 对象存储接入。
8. REST API Profile 登记与只读探查能力（具体认证方式和分页/限流能力 TBD）。

### 4.3 Pipeline Studio

**需求输入与澄清区**

- 自然语言需求输入。
- Source / Sink 选择。
- 展示 LangGraph 状态、缺失参数、澄清问题与恢复状态。
- 明确显示 Agent 正在做的只读探查和生成动作。
- 对外部 LLM 数据发送展示脱敏/授权状态。

**方案配置与拓扑审查区**

- 展示版本化 EtlPlan DSL。
- 展示 DAG 拓扑、Source / Transform / Sink 节点。
- 展示 SQL Transformation 和执行位置选择理由。
- 展示 SeaTunnel HOCON。
- 展示 Schema 对齐、字段映射 Diff、敏感分类和脱敏前后结构对比。
- 展示 QualityContract、RuntimeSupervisionContract。
- 支持用户编辑允许范围内的 SQL / 配置；编辑结果必须重新经过确定性门禁。

**门禁与提交区**

- 执行 Schema 校验、SQL AST 校验、Connector 能力校验、HOCON 解析/编译验证和策略校验。
- 显示错误、Warning、修复建议和修复轮次。
- 修复循环必须有上限；上限值 TBD。
- 通过后冻结不可变 PipelineVersion 和 SHA256 Manifest。
- 显示制品依赖与摘要。
- 允许配置 Schedule，但不能绕过 Prepare / Approve / Commit。

### 4.4 运行中心与审批控制台

**Preparation 准备单**

显示：输入指纹、Pipeline Artifact Hash、Metadata Snapshot、资源范围、数据分级、预算、执行环境、Secret 引用/版本、回滚方案、风险等级、审批槽位和失效条件。

**审批面板**

- Checker 看到冻结事实，不直接修改 Prepare 内容。
- Data Checker 审查 Schema、映射、脱敏结构、DAG、QualityContract。
- Security Checker 审查资源范围、数据分类、预算、Secret 引用、工具权限和恢复计划。
- 所有审批附带主体、时间、决策、原因和事实摘要。
- 事实发生策略定义的重大变化时，Preparation 失效并要求重新 Prepare / Approve。

**执行监控与质量诊断**

展示：ExecutionRun、SeaTunnel Job ID、状态、行数、字节数、吞吐量、耗时、放大比、整体/窗口拒绝率、错误码分布、日志、Trace、质量报告。

支持受管取消、暂停（若引擎/模式支持）、重启、恢复和回滚；界面必须区分“请求已提交”“执行引擎已确认”和“状态待对账”。

### 4.5 安全治理与 Benchmark

- Benchmark 数据集版本管理。
- L0 静态注入、L1 模拟故障、L2 真实链路测试。
- 模型 Provider / Model / Prompt / Generator 版本比较。
- PDP 策略版本回归。
- 原始指标、综合评分、失败用例和历史趋势。
- P0/P1 安全门槛作为独立硬性门槛，不能被生成准确率抵消。
- 安全策略更新需保留版本、审批、差异和回滚记录。

---

## 5. 核心业务流程

### 5.1 需求生成与受管发布

1. Maker 选择 Project、Source、Sink。
2. 创建或引用受管连接；Secret 不返回明文。
3. 发起只读元数据探查，生成 Metadata Snapshot。
4. 在 Studio 输入自然语言需求。
5. LangGraph 解析需求；必要参数缺失时中断，写入 PostgreSQL Checkpoint。
6. Maker 补充信息，Graph 从 Checkpoint 恢复。
7. Agent 生成 EtlPlan / SQL / HOCON 候选。
8. Compiler、SQL AST Validator、HOCON Validator、Schema Gate、Connector Capability Gate 执行确定性校验。
9. 失败时进入有限修复循环；仍失败则交还 Maker。
10. 通过后冻结 PipelineVersion 和 Artifact Manifest。
11. Maker 发起 Prepare；PDP 计算风险、资源范围、预算、指纹和审批槽位。
12. Data Checker 与 Security Checker 独立审批；职责分离规则强制执行。
13. Operator 发起 Commit。
14. Tool Broker 重新校验制品指纹、Preparation 状态、审批摘要、策略版本和执行预算。
15. Capability 签发与单次消费事实、ExecutionRun、Outbox 命令按事务协议持久化。
16. Outbox Dispatcher 投递 Celery 命令。
17. Worker 验证已授权命令、获取 Secret、调用 SeaTunnel Zeta。
18. SeaTunnel 将数据写入影子表/暂存区，QualityContract 将违规数据分流。
19. Runtime Supervision 检查预算与拒绝率；超限时中断。
20. 质量门禁通过后执行目标端发布策略；Doris 使用原子表替换能力。
21. 更新 PublishedDatasetVersion、ExecutionRun、质量报告和 Evidence Ledger。

### 5.2 超限中断与回滚

1. 监督引擎检测整体拒绝率、滚动窗口拒绝率或绝对错误数超限。
2. 触发受管中断命令，Worker 请求停止 SeaTunnel Job。
3. 记录中断原因、最后可确认指标、Job ID、位点/检查点（若适用）和失败快照。
4. 生成诊断解释和建议；诊断建议本身不产生副作用。
5. Operator 发起受管回滚。
6. Harness 验证 Operator 权限、目标 PublishedDatasetVersion、回滚预算和资源范围。
7. Worker 执行影子表清理、Swap 恢复或 CDC 补偿步骤。
8. 产生新的审计事件，不删除原始执行和审计记录。

---

## 6. 质量、运行监督与指标

### 6.1 QualityContract

规则候选范围：NOT_NULL、DATA_TYPE、REGEX、RANGE、UNIQUE、ENUM、受限 SQL 质量查询。规则失败的数据进入错误表，记录原始值（按数据分级策略保护）、规则 ID、ErrorCode、原因摘要和发生时间。

用户选择 SQL 仅允许 SELECT；仍必须执行 AST 白名单、对象范围约束、只读连接、函数白名单、超时及资源预算。SQL 的具体语法子集、支持方言、函数白名单和样本脱敏展示规则为 `TBD`。

### 6.2 RuntimeSupervisionContract

冻结：最大读取行数、最大写入字节数、最大时长、输出放大比、整体拒绝率、滚动窗口拒绝率、绝对错误行数。

拒绝率口径：

`reject_rate = error_rows / validated_rows`

已确认拒绝率阈值为 20%；“超过 20%”触发阈值的边界含义按字面处理，正好 20% 不因该比例条件单独触发。最小统计样本量、滚动窗口长度、绝对错误上限和窗口策略仍为 `TBD`。

### 6.3 指标

- Read Rows / Read Bytes。
- Validated Rows。
- Valid Rows / Write Rows / Write Bytes。
- Error Rows / Reject Rate。
- Throughput、Duration、Amplification Ratio。
- CDC Lag、Checkpoint Position、Restart Count（仅 CDC）。
- Connector / Zeta Job 状态。
- 资源预算使用率。

### 6.4 Benchmark 指标

至少包含 Plan Validity、HOCON Validity、Schema Mapping Accuracy、SQL Safety Detection、P0 Block Rate、P1 Block Rate、Replay Prevention、Repair Success Rate、End-to-End Success。保留原始指标，并按数据生成、安全治理、执行可靠性三个维度生成综合评分。权重和阈值均为 `TBD`；P0/P1 硬门槛不得被综合分数抵消。

---

## 7. 非功能需求

### 7.1 安全

- 多 Organization / Project 隔离，应用层过滤与 PostgreSQL RLS 双重保护。
- 生产运行账号不拥有业务表，不具有 `BYPASSRLS`。
- Agent 仅能调用受限只读工具和生成/校验工具。
- 所有副作用经 Tool Broker。
- Secret 受管存储，日志、HOCON、Manifest、Prompt 和 Evidence Ledger 不包含凭据明文。
- 外部 LLM 默认仅接收必要 Schema 信息和脱敏样本；扩大数据范围必须有项目策略授权与审计。
- 本地文件只允许上传目录、配置挂载目录和受控对象存储。
- SQL 沙箱不可仅依赖字符串黑名单。
- Capability 使用 Ed25519、短时有效和单次消费记录。
- Evidence Ledger 使用 SHA256 链与 Ed25519 签名检查点；不宣称具备外部 WORM 防删除能力。

### 7.2 可观测性

集成 Prometheus、Grafana、Loki、OpenTelemetry Collector、Grafana Tempo。Metrics、Logs、Traces、Audit Evidence 分层保存并通过 Trace ID / ExecutionRun ID 关联。

### 7.3 可恢复性与一致性

- PostgreSQL 保存业务事实和 LangGraph Checkpoint。
- Outbox 可靠投递，Celery 允许至少一次消息投递。
- ExecutionRun 和任务命令幂等。
- 外部 SeaTunnel 提交结果不确定时进入待对账状态，禁止盲目重提。
- 批处理与 CDC 使用不同恢复语义。

### 7.4 本地运行约束

- Windows 11 Pro 主运行机：32GB 物理内存。
- Docker 资源预算：最大 20GB。
- 可用磁盘预算：150GB。
- Windows 11：开发交互端。最终docker部署运行。
- 单个 Docker Compose 文件，服务默认启动。
- 默认 SeaTunnel：单节点可运行；保留多节点扩展配置。
- 默认 Doris：单 FE + 单 BE；保留可扩展配置。
- Vault：Development Profile 和持久化 Profile。

所有容器必须进行资源限制；是否能在 20GB 内同时运行全部服务必须通过启动与压力测试验证，不能仅凭配置宣称通过。

### 7.5 演示数据

支持 10,000、100,000、1,000,000 行三档确定性数据集。使用固定种子生成正常数据与异常数据，确保 Benchmark 可重复。

---

## 8. 验收标准

### 8.1 身份与权限

- 可创建预置演示用户并加入多个 Organization / Project。
- 项目角色与自定义角色生效。
- RBAC + ABAC 在 API 和后台任务中执行。
- RLS 越权测试通过；普通 Runtime Role 不可绕过 RLS。
- Maker 自批和审批职责混用被拒绝。

### 8.2 数据源与元数据

- 至少 MySQL、PostgreSQL、Doris 演示环境完成真实连接与探查。
- 连接器能力矩阵逐项记录各数据源与同步模式的真实验证结果。
- Schema、统计、脱敏样本、敏感标记和 Drift 记录可查看。
- Secret 不出现在 API、日志、前端和制品中。

### 8.3 Agent 与制品

- 自然语言需求可触发 LangGraph Graph。
- 缺少参数时中断；补充后通过 PostgreSQL Checkpoint 恢复。
- 输出 EtlPlan DSL、SQL、HOCON 和 DAG。
- 确定性门禁可拒绝非法 Schema、SQL、HOCON 和未支持 Connector 配置。
- 通过后生成不可变版本号、Manifest 和 SHA256。

### 8.4 Harness 与审批

- Prepare、Approve、Commit 三阶段可完整执行。
- 指纹变化按策略使 Preparation 失效或重新评估。
- P0 拒绝；P1 双审批；P2 单审批；P3 简化处置。
- Ed25519 Capability 有效期 5 分钟，绑定工具、主体、环境、制品和审批摘要。
- Capability 单次消费依赖 PostgreSQL 唯一约束；Redis 作为快速防重放层。
- Outbox、ExecutionRun 和 Capability 消费事实按明确事务边界持久化。

### 8.5 数据执行

- MySQL → Doris 核心批处理真实执行。
- MySQL CDC → Doris 真实链路作为首要 CDC 验收目标。
- PostgreSQL CDC → Doris 完成独立集成验证，记录位点、删除事件、Schema 变化和恢复限制。
- 有效记录写入目标影子表，违规记录进入错误表。
- Doris 发布前执行 Schema 兼容检查，并验证原子替换。
- 发布后可定位 PublishedDatasetVersion 并执行受管恢复。

### 8.6 运行监督与回滚

- 行数、字节数、时长、放大比、拒绝率持续可见。
- 20% 拒绝率策略能触发超限路径；具体统计窗口和最小样本参数待定。
- SeaTunnel 中断、未知提交结果和 Worker 重启均进入明确状态，禁止重复副作用。
- 可生成可解释诊断和受管回滚记录。

### 8.7 可观测性与 Benchmark

- Grafana 能查看 Prometheus Metrics、Loki Logs 和 Tempo Traces。
- L0/L1/L2 Benchmark 可运行。
- 模型与策略版本可比较。
- P0/P1 安全门槛失败时 Benchmark 总体判定失败。
- 三档数据集均可生成并记录结果。

---

## 9. 未决事项（TBD，不得默认补齐）

| ID | 未决事项 | 影响 |
|---|---|---|
| PRD-TBD-01 | Oracle / ClickHouse / 文件 / REST API 各自的本地测试依赖与完整执行优先级 | 连接器范围、Compose 资源 |
| PRD-TBD-02 | 自定义 SQL 的 AST 白名单、函数白名单、SQL 方言和对象范围 | SQL 安全与可用性 |
| PRD-TBD-03 | Email OTP 是否仅作为演示二次验证，或后续升级 TOTP / WebAuthn | 身份保证等级 |
| PRD-TBD-04 | RLS 后台任务身份上下文的具体实现 | 多租户安全 |
| PRD-TBD-05 | Preparation 变化分类的最终规则和失效矩阵 | 审批有效性 |
| PRD-TBD-06 | Metadata 样本过期时间、统计刷新周期、Schema Drift 默认阈值 | 元数据治理 |
| PRD-TBD-07 | CDC 各 Connector 的版本、删除事件和故障恢复验收矩阵 | CDC 可靠性 |
| PRD-TBD-08 | Reject Rate 最小样本量、滚动窗口、绝对错误行数上限 | 运行监督 |
| PRD-TBD-09 | 批处理与 CDC 完整状态迁移表 | Worker / API / UI |
| PRD-TBD-10 | 历史数据、日志、PublishedDatasetVersion 和审计数据具体保留期限 | 磁盘与恢复成本 |
| PRD-TBD-11 | Cron 并发、错过调度、时区/DST 和 PipelineVersion 绑定默认值 | 调度语义 |
| PRD-TBD-12 | 20GB Docker 内存下所有默认启动服务的实测容量 | 本地部署可行性 |
| PRD-TBD-13 | Benchmark 各指标权重和非 P0/P1 阈值 | 综合评分 |
| PRD-TBD-14 | 执行预算数值、最大 SQL 时长、自动修复轮数 | 防止资源滥用 |
| PRD-TBD-15 | 目标部署是否仅本地 Docker Compose，还是还需云端部署模板 | 运维交付 |

---

## 10. 参考资料

- Google Antigravity — Connect over SSH: https://antigravity.google/docs/ssh
- Microsoft Learn — Accessing network applications with WSL: https://learn.microsoft.com/en-us/windows/wsl/networking
- Docker Docs — Docker Desktop WSL 2 backend: https://docs.docker.com/desktop/features/wsl
- Docker Docs — WSL 2 best practices: https://docs.docker.com/desktop/features/wsl/best-practices
- Apache SeaTunnel — Submitting Jobs to a Remote Zeta Cluster: https://apache.googlesource.com/seatunnel/+/dcf97867ac13c04661a35af8568f2e3e40d07ef1/docs/en/getting-started/submit-job-to-remote-zeta-cluster.md
- Apache Doris — Atomic Table Replacement: https://doris.apache.org/docs/4.x/data-operate/delete/atomicity-replace/
- NIST SP 800-63B: https://pages.nist.gov/800-63-4/sp800-63b.html
