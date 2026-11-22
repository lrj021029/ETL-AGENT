# M10 独立 QA 验收与 L0/L1/L2 基准评测报告

> **里程碑**：M10 — Independent Verification & PRD Traceability  
> **负责角色**：QA / Integration & Benchmark Verification Engineer (`qa`)  
> **日期**：2026-11-22  
> **文件所有权边界**：仅写入 `qa/**`、`tests/qa/**`、`docs/implementation/qa/**`，严格保持 `docs/PRD.md`、`docs/TECHNICAL_SPEC.md`、`contracts/**`、`backend/**`、`frontend/**`、`compose.yaml` 只读。

---

## 1. 交付物清单

1. **PRD 验收标准全量追溯矩阵**：`qa/ACCEPTANCE_MATRIX.md`
   - 完整覆盖 `docs/PRD.md` §8.1 ~ §8.7 共 35 条验收条款（`TC-IAM-01`~`05`、`TC-META-01`~`04`、`TC-AGENT-01`~`05`、`TC-HARNESS-01`~`06`、`TC-DATA-01`~`06`、`TC-SUP-01`~`04`、`TC-OBS-01`~`05`）及 §9 全部 15 项 `PRD-TBD-01`~`15` 未决参数化合规核查。
2. **真实跨模块 L0 安全注入与 L1 故障模拟评测套件**：`tests/qa/test_l0_l1_security_and_faults.py`
   - 加载真实 `benchmarks/cases/l0_security_cases.json`（8 项 P0/P1 安全攻击用例），逐条驱动真实合并业务模块（`ReadOnlyToolRegistry`、`sqlglot` SQL AST 沙箱、`PolicyEngine`、`sanitize_for_llm`、`CapabilityIssuer`/`CapabilityVerifier`/`ReplayGuard`、`validate_safe_file_path`、`ControlledFileBoundary`、`PreparationService`/`ApprovalEngine`、`ToolBroker`/`EvidenceLedger`），实测 **P0 拦截率 100% (5/5)、P1 拦截率 100% (3/3)**。
   - 加载真实 `benchmarks/cases/l1_fault_cases.json`（6 项故障模拟用例）及专项边界用例，逐条驱动真实 `ConnectionService`、`ToolBroker`、`OutboxDispatcher`、`DataPlaneWorkerDispatcher`、`SeaTunnelZetaAdapter`、`RunReconciler`、`RuntimeSupervisionEngine`（精确验证 **20.0% = 200/1000 不触发** vs **20.1% = 201/1000 触发硬中断**）、`DorisPublishAdapter`（Schema 不兼容阻断且零 DDL 执行）、`RollbackExecutor`（CDC C+D 位点丢失时未获 Operator 显式确认强制阻断 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`）。
   - 将 L0 + L1 实测结果输入真实 `benchmarks/runner/scoring.py`，验证 11 项原始指标完整保留、三维可配置评分以及 P0/P1 安全硬门槛独立一票否决机制。
3. **真实 Live Docker 容器 L2 端到端集成套件**：`tests/qa/test_live_docker_l2.py`
   - 自动探测宿主机 `127.0.0.1` 各服务端口；对已启动容器执行 100% 真实协议读写与攻击验证，对尚未启动的重型容器诚实使用 `pytest.skip("BLOCKED: <service> container not running")` 标记阻塞，绝不伪造 `PASS`。

---

## 2. 真实测试执行结果汇总

### 2.1 L0 静态安全注入与 L1 故障模拟套件（100% PASS）

- **执行命令**：
  ```powershell
  python -m pytest tests/qa/test_l0_l1_security_and_faults.py -v
  ```
- **实测结果**：**7 passed in 2.18s**
  - `test_l0_all_security_injection_cases_against_real_modules`: **PASSED**（`L0-SEC-001` ~ `L0-SEC-008` 全数由真实安全与编译模块拦截）
  - `test_l1_all_fault_simulation_cases_against_real_modules`: **PASSED**（`L1-FLT-001` ~ `L1-FLT-006` 全数符合预期恢复与阻断语义）
  - `test_l1_supervision_exact_20_0_percent_vs_20_1_percent_boundary`: **PASSED**（20.0% 保持 `RUNNING`，20.1% 触发 `HARD_INTERRUPT` 并生成只读诊断报告 `advisory_only=True, side_effects_executed=False`）
  - `test_l1_seatunnel_submit_unknown_and_run_reconciler_no_blind_resubmit`: **PASSED**（提交超时进入 `UNKNOWN` / `RECONCILIATION_PENDING`，重试不发第二次 HTTP POST，`RunReconciler` 对账恢复至 `RUNNING`）
  - `test_l1_rollback_executor_cdc_c_plus_d_operator_confirmation_block`: **PASSED**（CDC 检查点丢失且未获 Operator 确认时阻断并抛出 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`；Operator 显式确认后进入 `RECOVERING -> INITIAL_SNAPSHOT`；检查点有效时按策略 C 进入 `RECOVERING -> STREAMING`）
  - `test_l1_outbox_dispatcher_and_data_plane_worker_redelivery_idempotency`: **PASSED**（Outbox 租约投递 + Worker 收到重复投递时第二次返回 `IDEMPOTENT_NOOP`，外部 HTTP 提交仅发生 1 次）
  - `test_benchmark_engine_and_scoring_p0_p1_independent_hard_gate`: **PASSED**（P0/P1 100% 拦截时 `overall_passed=True`；任一 P0 或 P1 失败时即便综合得分 > 0.95 仍一票否决 `overall_passed=False`）

### 2.2 L2 真实 Docker 容器集成套件（5 PASSED, 3 SKIPPED/BLOCKED）

- **执行命令**：
  ```powershell
  python -m pytest tests/qa/test_live_docker_l2.py -v
  ```
- **实测结果**：**5 passed, 3 skipped in 16.09s**
  1. `test_l2_live_postgres_alembic_and_five_mandatory_rls_attacks` (`127.0.0.1:5432`): **PASSED**
     - 真实 PostgreSQL 16 容器完成 Alembic `0001_initial_schema` -> `0002_rls_policies` 迁移；
     - 实测核验 `etl_runtime` 角色 `rolbypassrls = false` 且在 `public` Schema 下拥有表数量为 `0`；
     - 5 项强制 RLS 负向隔离攻击（无上下文 Fail-Closed、跨组织查询返回 0 行、同组织跨项目查询返回 0 行、单连接池物理连接复用零跨租户残留、`resolve_execution_tenant_context` 受控派生与伪造 `command_id` 拦截）全部通过。
  2. `test_l2_live_vault_kv2_and_transit_ed25519_capability_jws` (`127.0.0.1:8200`): **PASSED**
     - 真实持久化 HashiCorp Vault 1.18 容器测试 `VaultKV2SecretProvider` 写入 v1、轮转 v2、按版本读取与删除；
     - 真实 `VaultTransitEd25519Signer` 调用 Vault Transit `etl-agent-capability` 密钥签发 Capability JWS，并导出 Vault Ed25519 公钥至 `KeyRing` 完成 `CapabilityVerifier` 验签。
  3. `test_l2_live_redis_replay_guard_set_nx_ex` (`127.0.0.1:6379`): **PASSED**
     - 真实 Redis 7 容器测试 `RedisFastPath` (`SET NX EX 360`) 首次预占成功并设置 TTL，第二次重放同一 `jti` 立即抛出 `CAPABILITY_REPLAY_DETECTED`。
  4. `test_l2_live_mailpit_email_otp_delivery_and_verification` (`127.0.0.1:1025` & `127.0.0.1:8025`): **PASSED**
     - 真实通过 Mailpit SMTP 发送“邮件二次验证”OTP 邮件，调用 Mailpit HTTP API (`/api/v1/messages`) 核验邮件正文包含该 6 位验证码与“邮件二次验证”标识，并完成登录换取 JWT。
  5. `test_l2_live_mysql_and_demo_postgres_metadata_probe_cdc_and_drift` (`127.0.0.1:3306` & `127.0.0.1:5433`): **PASSED**
     - 真实连接 MySQL 8.0 (`demo_ecommerce`) 验证 `@@log_bin = 1`、`@@binlog_format = ROW`，通过 `SqlAlchemyReadOnlyMetadataProber` 探查 `users`、`orders`、`raw_event_logs` 生成不可变 `MetadataSnapshot`；
     - 真实连接 Demo PostgreSQL 16 (`demo_orders`) 验证 `SHOW wal_level = logical`，探查 `pg_fulfillment_orders` 生成不可变 `MetadataSnapshot`；
     - 验证连续两次快照产生不同 `snapshot_id` 与 `schema_fingerprint`，并持久化 `SchemaDriftEvent`。
  6. `test_l2_live_minio_bucket_tenant_upload_and_sha256_verification` (`127.0.0.1:9000`): **SKIPPED (`BLOCKED: minio container not running on 127.0.0.1:9000`)**
  7. `test_l2_live_doris_shadow_table_schema_gate_atomic_swap_and_rollback` (`127.0.0.1:9030`): **SKIPPED (`BLOCKED: doris-fe container not running on 127.0.0.1:9030`)**
  8. `test_l2_live_seatunnel_zeta_cluster_health_and_job_lifecycle_query` (`127.0.0.1:5801`): **SKIPPED (`BLOCKED: seatunnel-zeta container not running on 127.0.0.1:5801`)**

---

## 3. 独立验证中发现的真实容器环境细节记录（供 Lead 参考）

1. **PostgreSQL 16 `0002_rls_policies.py` 中的 `ALTER ROLE ... NOSUPERUSER` 权限要求**：
   - 在 PostgreSQL 16 中，`ALTER ROLE etl_runtime NOSUPERUSER ...` 子句即使在目标角色本来就不是 Superuser 时，也要求执行该语句的会话角色具备 `SUPERUSER` 属性，否则 PostgreSQL 抛出 `InsufficientPrivilege: Only roles with the SUPERUSER attribute may change the SUPERUSER attribute`。在 L2 集成测试中已通过 `postgres` 引导授权完成 `etl_migrator` 迁移验证。
2. **PostgreSQL RLS 策略中自定义 GUC 重置为空字符串 `''` 时的 `::uuid` 类型转换行为**：
   - 当连接上首次调用 `set_config('app.organization_id', '...', true)` 后，事务结束时 PostgreSQL 会将该自定义 GUC 重置为空字符串 `''`（而非全新连接时的 `NULL`）。由于 PostgreSQL 查询优化器不保证 `AND` 短路求值顺序，`organization_id = current_setting('app.organization_id', true)::uuid` 在无上下文查询时可能先尝试将 `''` 转为 `uuid` 并抛出 `DataError (InvalidTextRepresentation)`。虽然抛出异常同样实现了 Fail-Closed（零数据泄露），但若后续迭代将策略谓词写为 `NULLIF(current_setting('app.organization_id', true), '')::uuid`，则无论是全新连接（`NULL`）还是复用连接重置后（`''`），无上下文 `SELECT` 都能统一安静返回 `0` 行。
