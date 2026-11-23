# ETL-Agent 企业级异构数据集成平台 — 用户操作与全链路验收说明书

> **适用版本**：v1.0.0（本地实机联调与全功能验收版）  
> **阅读对象**：项目使用者、测试与验收工程师、演示人员  
> **核心原则**：本平台严格遵循 **Plan / Prepare / Approve / Commit / Execute** 五阶段受管架构；任何 Agent 推理仅做只读探查与草稿生成，所有外部写副作用必须经由确定性门禁、双人 SoD 审批及 Tool Broker 单次 Ed25519 Capability 签发。

---

## 1. 服务入口与演示账号一览

### 1.1 本地实机访问地址

| 服务模块 | 访问 URL | 用途说明 |
|---|---|---|
| **Vue 3 企业控制台（主入口）** | http://127.0.0.1:5173 | 全部 7 大业务视图与实时流交互界面 |
| **Mailpit 邮件收件箱（OTP 验证码）** | http://127.0.0.1:8025 | 捕获登录时的 **6 位邮件二次验证码（Email OTP）** |
| **FastAPI 控制面 API 文档** | http://127.0.0.1:8000/docs | Swagger 交互式接口文档（38 条契约路由） |
| **Grafana 可观测性大盘** | http://127.0.0.1:3000 | Prometheus + Loki + Tempo 三位一体大盘（默认 `admin` / `admin`） |
| **MinIO 对象存储控制台** | http://127.0.0.1:9001 | 租户隔离文件上传对象管理 |
| **Prometheus 指标查询** | http://127.0.0.1:9090 | 控制面 12 项契约指标实时采集 |

### 1.2 六大预置演示账号（RBAC + SoD 职责分离）

> 所有演示账号已持久化写入真实 PostgreSQL 16 (`etl_control_plane`)，**统一密码：`DemoPass!2026`**。

| 邮箱账号 | 角色定位 | 可执行的核心操作 | 安全边界（会被后端强拒的操作） |
|---|---|---|---|
| `maker@etl-agent.local` | **Maker（研发 / 发起人）** | 数据源探查、Studio 编排、冻结版本、发起 `Prepare` | **严禁审批自己发起的 Prepare**（触发 `APPROVAL_SELF_APPROVE_FORBIDDEN`） |
| `data_checker@etl-agent.local` | **Data Checker（数据审批员）** | 审批生产变更的 `DATA_CHECKER` 槽位 | **严禁兼任 `SECURITY_CHECKER` 槽位**（触发 `APPROVAL_SLOT_CONFLICT`） |
| `security_checker@etl-agent.local` | **Security Checker（安全审批员）** | 审批生产变更的 `SECURITY_CHECKER` 槽位 | 不可代签已被他人或自己占用的互斥槽位 |
| `operator@etl-agent.local` | **Operator（运维执行员）** | 携带 `Idempotency-Key` 提交 `Commit`、取消/对账/发起回滚 | 不可绕过未完成的审批槽位强行 Commit（触发 `APPROVAL_INCOMPLETE`） |
| `auditor@etl-agent.local` | **Auditor（只读审计员）** | 查看审计事件、执行 Evidence Ledger SHA256 链 + Ed25519 验签 | 全程只读，无权修改或执行作业 |
| `admin@etl-agent.local` | **Org/Project Admin（管理员）** | 组织/项目全局配置与数据源连接管理 | 同样受 SoD 约束，不可单人包办审批双槽 |

---

## 2. 启动与日常运维命令（Windows PowerShell）

如果重启电脑后需要重新拉起整套系统，按以下 3 步执行即可（在 `d:\etl-agent` 目录下）：

```powershell
# 1. 确保工具链在 PATH 中并启动全部 15 个 Docker 基础设施容器（持久化模式）
$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;D:\code\nodejs\node_global;$env:PATH"
docker compose up -d
docker ps --format "table {{.Names}}\t{{.Status}}"   # 确认 15 个容器均为 healthy

# 2. 启动 FastAPI 控制面后端（端口 8000）
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

# 3. 新开一个终端，启动 Vue 3 前端控制台（端口 5173）
$env:PATH = "D:\code\nodejs;D:\code\nodejs\node_global;$env:PATH"
pnpm --dir frontend dev --host 127.0.0.1 --port 5173
```

> **提示**：如曾重置过 PostgreSQL 数据卷，运行一次幂等初始化脚本即可重新灌入 6 个演示账号、2 组织、3 项目及 MySQL/Doris 真实连接：
> ```powershell
> python -m alembic -c backend/alembic.ini upgrade head
> python scripts/dev/seed_demo.py
> ```

---

## 3. 完整 7 步手工验收操作指南（逐页面走查）

以下 7 个步骤覆盖平台 100% 的核心产品能力与安全红线，建议按顺序在浏览器中完整演练一遍（约 10–15 分钟）。

---

### 步骤 1：登录与多租户上下文验证（AuthView + OverviewView）

1. 打开浏览器访问 http://127.0.0.1:5173 。
2. 输入 Maker 账号 `maker@etl-agent.local` 和密码 `DemoPass!2026`，点击 **「登录并发送邮件验证码」**。
   - **预期结果**：页面进入 **邮件二次验证（Email OTP）** 输入框，提示验证码已发送至脱敏邮箱。
3. 新开浏览器标签访问 Mailpit（http://127.0.0.1:8025 ），点开最新一封标题含 OTP 的邮件，复制 **6 位数字验证码**。
4. 回到前端填入 6 位验证码并提交。
   - **预期结果**：自动跳转至 **「项目总览工作台」**。
   - 顶部租户栏应正确显示组织 **`Alpha 电商数据团队`**、项目 **`电商订单宽表同步项目 (Prod)`**（可切换至 Staging / Beta 组织）。
   - 核心指标卡片 **「数据源连接健康度」** 应清晰显示 **`2 / 2`**（已探查可用连接数 / 总连接数）。

---

### 步骤 2：数据源连接、真实只读 Metadata Probe 与脱敏验证（ConnectionsView）

1. 点击左侧导航 **「数据源与资产管理」**。
   - **预期结果**：立即加载出 2 条真实连接卡片（无卡顿、无明文密码）：
     - **`MySQL 电商订单源库 (demo_ecommerce)`** — `MYSQL · 127.0.0.1:3306/demo_ecommerce`，状态 `OK`，Secret 引用显示 `******`。
     - **`Apache Doris 数仓目标库 (etl_warehouse)`** — `DORIS · 127.0.0.1:9030/etl_warehouse`，状态 `OK`，Secret 引用显示 `******`。
2. 点击右上角 **「连接器能力矩阵 (12)」**：
   - **预期结果**：右侧滑出抽屉，展示全部 12 种连接器的真实版本、支持模式（BATCH / CDC）与诚实状态（`CONFIGURED` / `PLANNED`，绝不虚标未验证的 `END_TO_END_VERIFIED`）。
3. 点击 `MySQL 电商订单源库 (demo_ecommerce)` 卡片上的 **「只读探活」**：
   - **预期结果**：顶部蓝色提示条返回 `探活结果 [MySQL 电商订单源库 (demo_ecommerce)]: 连通正常 (只读连通性探活通过)`。
4. 点击该卡片的 **「Metadata 快照 & Drift」** → 点击下方卡片右上角的 **「发起只读 Probe」**：
   - **预期结果**：
     - 后端自动从持久化 Vault KV v2 读取只读凭据，直连真实 MySQL 8.0 (`127.0.0.1:3306/demo_ecommerce`)；
     - 页面渲染出真实表（如 `demo_ecommerce.orders`、`demo_ecommerce.users`）、列类型、主键 `PK` 徽章、确定性 `Schema Fingerprint (sha256:...)`；
     - 敏感列（如 `customer_phone` / `customer_email`）已被自动识别并标记敏感分级，脱敏样本中绝不暴露明文 PII。
5. 任意修改某一列的「敏感分级」下拉框（如改为 `CONFIDENTIAL`）：
   - **预期结果**：提示 `已更新敏感分级`，数据库持久化记录 `MANUAL_MARK`。
6. （可选）点击右上角 **「上传文件至 MinIO」** 上传一个本地 `.csv` 文件：
   - **预期结果**：顶部绿色提示条显示租户隔离的对象路径 `etl-artifacts/org-.../proj-.../...`。

---

### 步骤 3：Pipeline Studio — LangGraph 编排、确定性门禁与版本冻结（PipelineStudioView）

1. 点击左侧导航 **「Pipeline Studio」**。
2. 在左区 **「1. 需求输入与 LangGraph 澄清」**：
   - 在目标 Pipeline 下方输入框填入名称（如 `mysql-orders-to-doris`），确认 Source 为 `MySQL 电商订单源库`、Sink 为 `Apache Doris 数仓目标库`，点击 **「+ 新建」**。
   - 在「自然语言集成需求」文本框输入：
     > `将 MySQL demo_ecommerce.orders 表全量同步到 Doris etl_warehouse.orders_target，对敏感字段脱敏并开启 20% 拒绝率熔断`
   - 点击 **「生成候选 EtlPlan / SQL / HOCON」**。
   - **预期结果**：
     - 顶部展示 `外部 LLM 脱敏: 已脱敏 (v1.0.0)`；
     - 如果 LangGraph 判定缺少必填参数（如 `source_table` / `sink_table`），左侧会出现黄色 **「需要补充澄清参数 (PG Checkpoint)」** 表单，填入对应表名（例如 `demo_ecommerce.orders` / `etl_warehouse.orders_target`）并点击 **「提交澄清并恢复 Graph」**；
     - 完成后中间区域 5 个 Tab（`DAG 拓扑`、`EtlPlan DSL`、`SQL & HOCON`、`映射 & 脱敏 Diff`、`质量与监督契约`）自动填充完整结构化产物。
3. 在右区 **「3. 确定性门禁与版本冻结」**：
   - 点击 **「执行确定性门禁校验」** → **预期结果**：`SCHEMA`、`SQL_AST`、`HOCON`、`CONNECTOR_CAPABILITY`、`POLICY` 5 项门禁校验报告返回并显示 `GATE PASSED`（若手动在 SQL 编辑器输入 `DROP TABLE` 或 `DELETE` 并重跑门禁，将立刻被 `sqlglot` AST 沙箱拦截并显示 `GATE FAILED`）。
   - 点击 **「冻结不可变 PipelineVersion」** → **预期结果**：生成不可变版本 `v1` 及确定性 `SHA256` 摘要。
   - 点击 **「发起 Harness Prepare 审批单」** → **预期结果**：创建 P1 风险等级的 `Preparation`（零外部写副作用，自动分配 `DATA_CHECKER` + `SECURITY_CHECKER` 双独立审批槽位）。

---

### 步骤 4：审批控制台 — 验证 SoD 红线拦截、独立双签与 Operator Commit（ApprovalConsoleView）

> **核心验收亮点**：这里将亲眼验证「职责分离（SoD）」由后端强校验，而非仅靠前端隐藏按钮。

1. **验证 Maker 自批被拒（负向安全验收）**：
   - 当前仍以 `maker@etl-agent.local` 登录，点击左侧 **「审批控制台与 Preparation」**。
   - 点击刚生成的 Preparation 右侧 **「审查冻结事实与审批」**，查看冻结的 SHA256 指纹、资源范围、预算与回滚方案。
   - 尝试点击 **「通过 (DATA_CHECKER)」**。
   - **预期结果**：页面顶部出现红色后端原文拦截告警：**`APPROVAL_SELF_APPROVE_FORBIDDEN`**（Maker 严禁自批）。
2. **完成第 1 槽位审批（Data Checker）并验证双槽互斥**：
   - 点击右上角退出登录，使用 **`data_checker@etl-agent.local`** / `DemoPass!2026`（同样去 Mailpit 取 OTP）重新登录。
   - 进入「审批控制台与 Preparation」→ 打开同一张准备单 → 填写审批意见并点击 **「通过 (DATA_CHECKER)」** → **预期结果**：`DATA_CHECKER` 槽位变为绿色 `APPROVED`。
   - **负向测试**：用同一个 `data_checker` 账号尝试点击 **「通过 (SECURITY_CHECKER)」** → **预期结果**：后端强拒并返回 **`APPROVAL_SLOT_CONFLICT`**（同一人不可兼任双审批槽位）。
3. **完成第 2 槽位审批（Security Checker）**：
   - 退出登录，切换 **`security_checker@etl-agent.local`** / `DemoPass!2026` 登录。
   - 打开同一张准备单，点击 **「通过 (SECURITY_CHECKER)」** → **预期结果**：两个槽位均变为 `APPROVED`，准备单总状态自动晋升为 **`APPROVED`**。
4. **Operator Commit 签发单次 Ed25519 Capability**：
   - 退出登录，切换 **`operator@etl-agent.local`** / `DemoPass!2026` 登录。
   - 打开该已批准准备单，点击底部 **「Operator Commit（重验指纹并签发单次 Capability）」** → 确认弹窗中的 `Idempotency-Key` 并点击 **「确认 Commit」**。
   - **预期结果**：绿色卡片显示受理成功，返回 `Execution ID`、`Command ID` 以及 `REQUEST_SUBMITTED` 确认状态；后端在同一数据库事务内原子落库 Capability 签发/消费记录、Outbox 事件与 Evidence Ledger 证据链事件。

---

### 步骤 5：运行中心与质量诊断 — 实时流、对账与受管回滚（ExecutionCenterView）

1. 点击左侧 **「运行中心与质量诊断」**。
   - **预期结果**：`BATCH 批处理` 列表显示刚刚 Commit 生成的 `ExecutionRun`。
2. 点击该运行记录：
   - **预期结果**：右侧加载执行详情、SSE 实时流通道状态（含 HTTP 轮询自动降级）、`confirmation_state` 显式确认徽章与 ECharts 吞吐/拒绝率折线图。
3. 点击 **「触发对账 (Reconcile)」** 或 **「发起受管回滚 Prepare」**：
   - **预期结果**：回滚不会直接执行任何 SQL，而是生成一条 `ROLLBACK` 类型的 `Preparation` 进入审批控制台，确保回滚同样受 Harness 审批与审计管控。

---

### 步骤 6：安全治理、防篡改证据链验签与 Benchmark 评测（GovernanceBenchmarkView）

1. 点击左侧 **「安全治理与 Benchmark」**（可使用当前账号或 `auditor@etl-agent.local` 只读视角）。
2. **Evidence Ledger 防篡改验签**：
   - 左侧展示所有操作累积生成的不可变 Evidence 事件流（带递增 `#seq` 与 `sha256_hash`），右侧展示 Ed25519 签名的 Checkpoints。
   - 点击右上角 **「校验 SHA256 链与 Ed25519 签名」**。
   - **预期结果**：绿色 banner 返回 `Hash Chain: true | Ed25519 Checkpoints: true` 与已核验事件总数。
3. **确定性 Benchmark 与 P0/P1 一票否决硬门槛**：
   - 选择规模 `10K`、随机种子 `42`，点击 **「生成确定性数据集」**。
   - 点击 **「触发 L0」** 或 **「触发 L1」**。
   - **预期结果**：下方 ECharts 柱状/折线对比图与表格立即渲染 `Plan Validity`、`SQL Safety`、`P0 Block Rate`、`HARD GATE PASS` 及 `Composite Score`。

---

### 步骤 7：自动化实机数据面验收（真实 MySQL → SeaTunnel Zeta → Doris 原子切换与回滚）

若需一键验收最硬核的 **L2 真实数据搬运、Schema Gate、Doris 原子 Swap 与回滚**（零 Mock），可在终端直接运行：

```powershell
# 1. 单独运行真实 MySQL 8.0 -> SeaTunnel Zeta 2.3.9 -> Doris 2.1.7 搬运/原子 Swap/回滚实测（约 5 秒）
python -m pytest tests/qa/test_live_seatunnel_mysql_to_doris_e2e.py -v

# 2. 运行全仓库 264 项后端回归（含真实 PG 16 五项 RLS 渗透攻击、真实持久化 Vault Transit/KV v2、Redis、MinIO、Mailpit、SeaTunnel、Doris）
$env:ETL_TEST_PG_RUNTIME_URL = "postgresql+psycopg://etl_runtime:dev_only_runtime_placeholder_not_for_prod@127.0.0.1:5432/etl_control_plane"
python -m pytest -q --tb=short

# 3. 运行前端 TypeScript 类型检查与单元测试
$env:PATH = "D:\code\nodejs;D:\code\nodejs\node_global;$env:PATH"
pnpm --dir frontend typecheck; pnpm --dir frontend test
```

---

## 4. 常见问题排查（FAQ）

| 现象 | 原因与解决方法 |
|---|---|
| 登录后看不到验证码 | 打开 Mailpit http://127.0.0.1:8025 刷新收件箱；确认 `etl-agent-mailpit` 容器处于 `healthy` 状态 |
| 前端提示网络错误或 `HTTP_500` | 检查 `http://127.0.0.1:8000/health` 是否返回 `200 OK`，确认后端 Uvicorn 进程在运行 |
| 电脑重启后 Vault 报 `sealed` / 403 | Vault 使用持久化 File Storage，重启容器后需按 `infra/vault/init/vault-init.json` 中的 unseal key 执行解封（或执行 `scripts/dev/init_vault.ps1`） |
| 电脑重启后 Doris FE 起不来 | `compose.yaml` 已固定 `doris-fe` (`172.19.0.10`) 与 `doris-be` (`172.19.0.14`) 静态 IP 及 `3584m` 内存上限；如遇磁盘异常请检查 `docker logs etl-agent-doris-fe` |
| Studio 点击门禁报错「缺少 EtlPlan」 | 请先点击「生成候选 EtlPlan / SQL / HOCON」并完成可能出现的澄清中断表单，再执行确定性门禁校验 |
