# Security / Harness 安全内核（Milestone M6）实现说明

## 1. 模块职责与文件结构

所有模块均位于 `backend/app/` 下（导入根路径为 `backend/`，未创建 `backend/app/__init__.py` 以免与 Backend 分支冲突）：

| 包路径 | 核心职责 |
|---|---|
| `backend/app/security/` | RFC 8785 JCS 兼容的 `canonical_bytes` / `canonical_digest` / `event_hash`（ADR-005）、日志与字典深度脱敏 `redaction.py`（保留 `${SECRET:...}` 占位符）、携带契约错误码的 `HarnessError` |
| `backend/app/secrets/` | `SecretProvider` 协议、`VaultKV2SecretProvider`（默认 Vault KV v2 实现）、`EncryptedPostgresSecretProvider`（基于 Fernet/MultiFernet 的应用层加密回退实现，绑定 `ref` 防跨引用密文替换并支持主密钥平滑轮转）、`SecretValue` 掩码封装 |
| `backend/app/policy/` | 版本化策略库 `PolicyStore`（`publish` / `diff` / 只追加 `rollback`，强制校验 SoD 不可关闭且 P0 黑名单不可清空）、RBAC+ABAC 决策引擎 `PolicyEngine.evaluate`（严格符合 `contracts/harness/pdp_contract.json`）、自定义角色 SoD 校验 `validate_role` |
| `backend/app/preparations/` | 零外部副作用的 `PreparationService.prepare`、确定性指纹 `fingerprint`、变更分级失效器 `classify_change`（区分阻塞级语义变更与纯展示字段）、受管定时调度 `StandingSchedulePreparation`（强制 `PINNED` 版本绑定与预算上限） |
| `backend/app/approvals/` | `ApprovalEngine`：支持 `DATA_CHECKER` / `SECURITY_CHECKER` / `SINGLE_CHECKER` / `AUTO_P3` 槽位，强制拦截 Maker 自批（`APPROVAL_SELF_APPROVE_FORBIDDEN`）、同人双槽冲突（`APPROVAL_SLOT_CONFLICT`）与 P0 审批（`PDP_DENIED_P0`），生成 `approval_digest` |
| `backend/app/capabilities/` | Ed25519 单次 Capability JWS 签发与验签（`alg=EdDSA`、携带 `kid`、TTL<=300s、完整校验 `capability_jws.schema.json` 的 15 项 Claims）、`LocalEd25519Signer`、`VaultTransitEd25519Signer`（自动将 Vault Transit 标准 Base64 64B 原始签名转换为 JWS Base64URL）、`KeyRing` 公钥轮转环、双层防重放 `ReplayGuard`（Redis `SET NX EX 360` 快速路径 + PostgreSQL `jti` 唯一约束） |
| `backend/app/broker/` | `ToolBroker`（唯一外部副作用出口）：提供 `commit`、`authorize_rollback`、`commit_scheduled`（`system:scheduler` 受管触发）、`handle_outbox` 与 `execute_command`，在同一原子事务内写入 7 类核心记录 |
| `backend/app/evidence/` | 仅追加哈希链证据账本 `EvidenceLedger`（逐字节对齐 `contracts/crypto/test_vectors.json`）、周期性 Ed25519 签名检查点 `checkpoint`、全链与检查点签名校验器 `verify_chain` |

## 2. Lead 集成绑定指南

1. **Commit 原子事务绑定**：`InMemoryBrokerStore.transaction()` 是 PostgreSQL 事务的参考模型。在集成阶段，将其绑定至单一数据库事务，原子涵盖：
   - `preparations.status` CAS 流转（`UPDATE preparations SET status='COMMITTED' WHERE preparation_id=:id AND status='PREPARED'`）
   - `capability_issuances`
   - `capability_consumptions(jti PRIMARY KEY)`（可直接复用 `SqlAlchemyConsumptionStore`）
   - `execution_runs`
   - `execution_commands(command_id UNIQUE)`
   - `outbox_events` 与 `outbox_deliveries`
   - `evidence_events`
2. **Worker 幂等抢占**：`claim_command(command_id)` 对应 `UPDATE execution_commands SET state='DISPATCHING' WHERE command_id=:id AND state='PENDING'`（判断 `rowcount == 1`）。Outbox 至少一次重复投递同一 `command_id` 时不会二次消费 Capability，也不会触发第二次外部副作用；若外部执行器异常则置为 `UNKNOWN` 等待对账，绝不盲目重跑。
3. **Vault Transit 与 Redis 绑定**：
   - `HvacTransitClient(hvac_client)` 传入 `VaultTransitEd25519Signer(client, "etl-agent-capability", key_version=1)`。
   - 公钥注册：`KeyRing.add_vault_public_key("etl-agent-capability:v1", vault_key_metadata["keys"]["1"]["public_key"])`。
   - Redis 快速防重放：`RedisFastPath(redis_client)` 传入 `ReplayGuard(fast_path, SqlAlchemyConsumptionStore(conn, table))`。
