# ADR-003: Harness 安全内核、单次 Capability 事务边界与受管 Schedule

- **状态**：APPROVED（2026-11-22）
- **决策者**：用户 + Principal Architect + Security

## 决策
1. **Tool Broker 唯一副作用出口**：
   - 任何外部副作用（SeaTunnel Submit/Stop、Doris Swap、Shadow Cleanup、Rollback）只能经 `Tool Broker` 触发。Agent 仅允许生成只读探查调用与结构化候选，不能调用 Broker Commit 或签发 Capability。
2. **P0–P3 策略与 P3 边界**：
   - `P0`：硬拒绝，不可通过审批放行。
   - `P1`：必须 `DATA_CHECKER` + `SECURITY_CHECKER` 双独立槽位审批（严禁 Maker 自批，严禁同一用户占双槽）。
   - `P2`：单审批槽位（严禁 Maker 自批）。
   - `P3`：策略允许时可简化/自动审批，但**仍强制走 `Prepare` 冻结 → `Tool Broker` 校验 → 5 分钟单次 Ed25519 Capability 签发与消费 → `Outbox` 事务**。
3. **Commit 原子事务边界（PostgreSQL 唯一事实）**：
   - Operator 发起 Commit（携带 `Idempotency-Key`）时：
     1. 服务端重验 Preparation 状态、指纹、`PipelineVersion` Hash、Metadata Fingerprint、Approval Digest、Policy Version 与预算。
     2. 签名服务签发 TTL=300s 的 Ed25519 JWS Capability Token（含 `kid`、`jti`、`nonce` 及完整上下文绑定 Claims）。
     3. Redis 快速防重放预检 `SET NX EX 360`。
     4. 在**单一 PostgreSQL 事务**中原子执行：插入 `capability_issuances`、插入 `capability_consumptions`（`jti` 唯一约束）、创建 `execution_runs`、创建 `execution_commands`（`command_id` 唯一约束）、插入 `outbox_events` + `outbox_deliveries`、追加 `evidence_events`。
   - Outbox Dispatcher 以 `SELECT ... FOR UPDATE SKIP LOCKED` + lease 投递 Celery（At-least-once）。Worker 重投同一 `command_id` 时做幂等检查与授权绑定验签，**不重复消费 Capability，也不产生第二次外部副作用**。
4. **受管 Cron Schedule (`StandingSchedulePreparation`)**：
   - 创建/修改 Schedule 必须针对固定 `pipeline_version_id` 发起受审 `StandingSchedulePreparation`（完整 Prepare + 对应风险等级审批 + 预算上限）。
   - Celery Beat 到点触发时，以受限系统身份 `system:scheduler` 调用 Tool Broker 的受管调度提交路径：重验 `StandingSchedulePreparation` 仍有效、无重大指纹漂移、满足并发/错过策略与预算后，实时签发当次 5 分钟单次 Ed25519 Capability 并走完全相同的原子 Commit + Outbox 事务。
   - Schedule 默认推荐策略（创建时可显式配置）：`concurrency_policy=FORBID`、`misfire_policy=SKIP`、`pipeline_version_binding=PINNED`。
