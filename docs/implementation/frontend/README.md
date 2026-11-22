# Frontend Implementation Guide (Milestone M9)

## 1. 技术栈与架构概览

- **核心框架**：Vue 3 + TypeScript + Vite + Pinia + Vue Router
- **样式体系**：Tailwind CSS + shadcn-vue 风格无障碍基础组件（支持深/浅色主题切换）
- **专项引擎**：
  - `@vue-flow/core`：Pipeline Studio DAG 拓扑可视化
  - `monaco-editor`：SQL / HOCON 编辑器与 Diff 审查视图
  - `echarts`：实时执行吞吐量、拒绝率与 Benchmark 多维度对比图表

## 2. 路由映射 (Route Map)

| 路由路径 | 视图组件 | 职责 |
|---|---|---|
| `/auth` | `AuthView.vue` | 密码登录 + 邮件二次验证 (Email OTP)，诚实标注合规边界 |
| `/overview` | `OverviewView.vue` | 实时 KPI 汇总、门禁概况、运行成功率/拒绝率/预算与最近审计事件 |
| `/connections` | `ConnectionsView.vue` | 连接 CRUD/探活、Secret 引用掩码、连接器能力矩阵抽屉、只读 Metadata 快照、Drift Diff、敏感标记、MinIO 上传 |
| `/studio` | `PipelineStudioView.vue` | PRD §4.3 三区布局：NL 需求/LangGraph 澄清恢复、DAG/DSL/SQL/HOCON/映射脱敏/契约、确定性门禁与不可变版本冻结、受管 Schedule |
| `/approvals` | `ApprovalConsoleView.vue` | Preparation 冻结事实抽屉、`DATA_CHECKER` / `SECURITY_CHECKER` 独立槽位、Operator Commit（带 `Idempotency-Key`）、SoD/P0 原文报错 |
| `/executions` | `ExecutionCenterView.vue` | Batch/CDC 分模式状态机、`RealtimeStreamAdapter`、`confirmation_state` 指示、质量报告、只读诊断、受管控制与回滚 Prepare |
| `/governance` | `GovernanceBenchmarkView.vue` | PDP 策略版本 Diff、Evidence Ledger SHA256 + Ed25519 Checkpoint 校验、10K/100K/1M 数据集、L0/L1/L2 运行对比与 P0/P1 独立硬门槛 |

## 3. API 客户端与实时流适配器

- **`frontend/src/api/client.ts`**：
  - 基础地址读取 `VITE_API_BASE_URL`（默认 `http://127.0.0.1:8000`）。
  - 自动注入 `Authorization: Bearer`、`X-Organization-Id`、`X-Project-Id` 与 `Idempotency-Key`。
  - 统一解析 `ErrorResponse`（`{ error_code, message, detail, trace_id }`）为 `ApiClientError`。
- **`frontend/src/api/realtime.ts`**：
  - `RealtimeStreamAdapter` 主通道使用 SSE (`EventSource`)，失败或不可用时自动降级为 `HTTP_POLLING`。
  - 显式暴露 `confirmation_state`（`REQUEST_SUBMITTED` / `ENGINE_CONFIRMED` / `RECONCILIATION_PENDING`）。
- **`frontend/src/components/ui/StateView.vue`**：
  - 在所有核心视图显式处理 `loading` / `empty` / `error` / `stale` / `partial` / `success` 六种状态。

## 4. 安全与合规约束

- 浏览器存储绝不保存数据库密码、LLM API Key 或 Capability 私钥。
- 邮件验证码统一标注为 **邮件二次验证**（不称抗钓鱼强 MFA）。
- 诊断建议明确标注为只读/无副作用。
- 前端不直接调用 SeaTunnel Zeta API。
