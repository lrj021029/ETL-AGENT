"""测试 3、4、5：
- 测试 3：`DorisPublishAdapter` 强制 Schema 兼容门禁测试（不兼容时在任何 `ALTER TABLE` 执行前阻断并抛出 `PUBLISH_SCHEMA_INCOMPATIBLE`；兼容时生成精确 `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')`）。
- 测试 4：`SeaTunnelZetaAdapter` & `RunReconciler` 故障注入测试（提交超时/响应丢失 -> `UNKNOWN`，严禁盲目重提；对账找到作业 -> `RUNNING`；重复 `command_id` -> 幂等 `IDEMPOTENT_NOOP`；绝不信任 Payload 中的租户 ID）。
- 测试 5：`RollbackExecutor` CDC C+D 恢复策略与批处理受管回滚测试（有效检查点 -> 恢复；`CHECKPOINT_LOST` / `POSITION_EXPIRED` 无 Operator 显式确认 -> 阻断 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`）。
"""
from __future__ import annotations

from typing import Any
import pytest

from backend.app.data_plane import (
    AuthorizedCommandFact,
    DataPlaneWorkerDispatcher,
    RunReconciler,
    SeaTunnelSubmitUnknownError,
    SeaTunnelZetaAdapter,
    build_stable_submit_id,
)
from backend.app.execution import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateMachine,
)
from backend.app.publishing import (
    DorisColumnSpec,
    DorisPublishAdapter,
    DorisTableSchemaSpec,
    PublishSchemaIncompatibleError,
    build_shadow_table_name,
)
from backend.app.rollback import (
    CdcRecoveryOperatorConfirmRequiredError,
    RollbackCommandContext,
    RollbackExecutor,
    RollbackType,
)


def _sample_target_schema() -> DorisTableSchemaSpec:
    """构建标准 Doris OLAP 目标表 Schema 规格。"""
    return DorisTableSchemaSpec(
        table_name="demo_dw.dwd_order_user_wide",
        engine_type="OLAP",
        keys_type="UNIQUE_KEYS",
        key_columns=["order_id"],
        partition_columns=["order_date"],
        columns=[
            DorisColumnSpec("order_id", "BIGINT", nullable=False, is_key=True),
            DorisColumnSpec("order_date", "DATE", nullable=False, is_key=False),
            DorisColumnSpec("user_id", "BIGINT", nullable=False, is_key=False),
            DorisColumnSpec("total_amount", "DECIMAL(18, 2)", nullable=False, is_key=False),
        ],
    )


def _sample_compatible_shadow_schema(exec_id: str = "exec001") -> DorisTableSchemaSpec:
    """构建与目标表完全兼容的影子表 Schema 规格。"""
    shadow_name = build_shadow_table_name("demo_dw.dwd_order_user_wide", exec_id)
    return DorisTableSchemaSpec(
        table_name=f"demo_dw.{shadow_name}",
        engine_type="OLAP",
        keys_type="UNIQUE_KEYS",
        key_columns=["order_id"],
        partition_columns=["order_date"],
        columns=[
            DorisColumnSpec("order_id", "bigint", nullable=False, is_key=True),
            DorisColumnSpec("order_date", "DATE", nullable=False, is_key=False),
            DorisColumnSpec("user_id", "BIGINT", nullable=False, is_key=False),
            DorisColumnSpec("total_amount", "decimal(18, 2)", nullable=False, is_key=False),
        ],
    )


# =============================================================================
# 测试 3：DorisPublishAdapter 强制 Schema 兼容门禁与原子表替换测试
# =============================================================================


def test_doris_publish_adapter_blocks_incompatible_schema_before_any_sql_runs() -> None:
    """验证列名缺失、类型不匹配、Nullability 不一致或非 OLAP 表均在执行任何 ALTER TABLE 前被阻断。"""
    executed_sqls: list[str] = []
    adapter = DorisPublishAdapter(sql_executor=lambda sql: executed_sqls.append(sql) or {})
    target = _sample_target_schema()

    # 场景 3.1：列数据类型不兼容（total_amount 从 DECIMAL(18, 2) 变为 VARCHAR(64)）
    bad_type_shadow = DorisTableSchemaSpec(
        table_name="demo_dw.dwd_order_user_wide__shadow_badtype",
        engine_type="OLAP",
        keys_type="UNIQUE_KEYS",
        key_columns=["order_id"],
        partition_columns=["order_date"],
        columns=[
            DorisColumnSpec("order_id", "BIGINT", nullable=False, is_key=True),
            DorisColumnSpec("order_date", "DATE", nullable=False, is_key=False),
            DorisColumnSpec("user_id", "BIGINT", nullable=False, is_key=False),
            DorisColumnSpec("total_amount", "VARCHAR(64)", nullable=False, is_key=False),
        ],
    )
    with pytest.raises(PublishSchemaIncompatibleError) as exc_info:
        adapter.publish_shadow_to_target(
            organization_id="org-1",
            project_id="proj-1",
            execution_id="exec-001",
            pipeline_version_id="pv-1",
            artifact_hash="sha256:111",
            target_connection_id="conn-doris",
            quality_report_id="qr-1",
            published_by="operator-1",
            target_schema=target,
            shadow_schema=bad_type_shadow,
        )
    assert exc_info.value.error_code == "PUBLISH_SCHEMA_INCOMPATIBLE"
    # 关键断言：阻断时绝不能有任何 SQL 语句被执行！
    assert executed_sqls == []
    assert adapter.executed_sql_log == []

    # 场景 3.2：Nullability 不兼容（user_id 变为 nullable=True）或缺失列
    bad_null_shadow = DorisTableSchemaSpec(
        table_name="demo_dw.dwd_order_user_wide__shadow_badnull",
        engine_type="OLAP",
        keys_type="UNIQUE_KEYS",
        key_columns=["order_id"],
        partition_columns=["order_date"],
        columns=[
            DorisColumnSpec("order_id", "BIGINT", nullable=False, is_key=True),
            DorisColumnSpec("order_date", "DATE", nullable=False, is_key=False),
            DorisColumnSpec("user_id", "BIGINT", nullable=True, is_key=False),
        ],
    )
    with pytest.raises(PublishSchemaIncompatibleError) as exc_info2:
        adapter.verify_schema_compatibility(target, bad_null_shadow)
    assert exc_info2.value.error_code == "PUBLISH_SCHEMA_INCOMPATIBLE"
    reasons = {m["reason"] for m in exc_info2.value.detail["mismatches"]}
    assert "MISSING_COLUMNS_IN_SHADOW" in reasons
    assert "COLUMN_NULLABILITY_MISMATCH" in reasons
    assert executed_sqls == []


def test_doris_publish_adapter_executes_exact_atomic_replace_swap_when_compatible() -> None:
    """验证 Schema 兼容门禁通过后生成精确的 `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')`。"""
    executed_sqls: list[str] = []
    adapter = DorisPublishAdapter(sql_executor=lambda sql: executed_sqls.append(sql) or {"ok": True})
    target = _sample_target_schema()
    shadow = _sample_compatible_shadow_schema("exec001")

    version_rec = adapter.publish_shadow_to_target(
        organization_id="org-1",
        project_id="proj-1",
        execution_id="exec001",
        pipeline_version_id="pv-10",
        artifact_hash="sha256:manifest10",
        target_connection_id="conn-doris",
        quality_report_id="qr-10",
        published_by="operator-alice",
        target_schema=target,
        shadow_schema=shadow,
        previous_version_id="pdv-prev-009",
    )

    expected_sql = (
        "ALTER TABLE demo_dw.dwd_order_user_wide "
        "REPLACE WITH TABLE dwd_order_user_wide__shadow_exec001 "
        "PROPERTIES('swap'='true')"
    )
    assert executed_sqls == [expected_sql]
    assert version_rec.target_table_ref == "demo_dw.dwd_order_user_wide"
    assert version_rec.shadow_table_ref == "demo_dw.dwd_order_user_wide__shadow_exec001"
    assert version_rec.archive_table_ref == "demo_dw.dwd_order_user_wide__shadow_exec001"
    assert version_rec.previous_version_id == "pdv-prev-009"
    assert version_rec.swap_result_json["swap_sql"] == expected_sql


# =============================================================================
# 测试 4：SeaTunnelZetaAdapter 提交响应丢失 -> UNKNOWN、RunReconciler 对账与 Worker 幂等
# =============================================================================


class _FaultInjectableZetaTransport:
    """支持模拟提交响应丢失（网络超时，但作业已在 Zeta 创建）的测试传输层。"""

    def __init__(self, *, timeout_on_submit: bool = False) -> None:
        self.timeout_on_submit = timeout_on_submit
        self.submit_call_count = 0
        self.received_hocon_bodies: list[str] = []
        self.cluster_jobs: list[dict[str, Any]] = []

    def post_submit_job(self, *, job_name: str, hocon_body: str) -> dict[str, Any]:
        self.submit_call_count += 1
        self.received_hocon_bodies.append(hocon_body)
        created_job_id = f"zeta-9900-{self.submit_call_count}"
        # 模拟作业实际已经在 Zeta 服务端创建并开始 RUNNING
        self.cluster_jobs.append(
            {
                "jobId": created_job_id,
                "jobName": job_name,
                "jobStatus": "RUNNING",
            }
        )
        if self.timeout_on_submit:
            raise TimeoutError("Simulated HTTP response lost after Zeta created job")
        return {"jobId": created_job_id, "jobName": job_name}

    def get_job_info(self, external_job_id: str) -> dict[str, Any]:
        for j in self.cluster_jobs:
            if j["jobId"] == external_job_id:
                return j
        raise KeyError(external_job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return list(self.cluster_jobs)

    def post_stop_job(
        self, external_job_id: str, *, is_stop_with_save_point: bool = False
    ) -> dict[str, Any]:
        return {"jobId": external_job_id, "stopped": True}


def test_seatunnel_submit_timeout_enters_unknown_and_reconciler_recovers_to_running() -> None:
    """验证提交超时/响应丢失时进入 `UNKNOWN`（绝不盲目重提），随后 `RunReconciler` 对账找回作业并恢复至 `RUNNING`。"""
    transport = _FaultInjectableZetaTransport(timeout_on_submit=True)
    adapter = SeaTunnelZetaAdapter(transport=transport)
    reconciler = RunReconciler(adapter)

    run = ExecutionRunRecord(
        execution_id="exec-lost-resp-01",
        organization_id="org-1",
        project_id="proj-1",
        pipeline_id="pipe-1",
        pipeline_version_id="pv-1",
        artifact_hash="sha256:abc",
        execution_mode=ExecutionMode.BATCH,
        preparation_id="prep-1",
        capability_jti="jti-1",
        command_id="cmd-lost-01",
    )
    hocon_tpl = 'env { job.mode = "BATCH" }\nsource { Jdbc { password = "${SECRET:DB_PASS}" } }'

    # 第一步：提交时发生响应丢失 -> 必须抛出 SeaTunnelSubmitUnknownError 并进入 UNKNOWN
    with pytest.raises(SeaTunnelSubmitUnknownError) as exc_info:
        adapter.submit_job(
            run=run,
            hocon_template=hocon_tpl,
            secret_resolver=lambda k: "SuperSecretValue123!",
        )
    assert exc_info.value.error_code == "SEATUNNEL_SUBMIT_UNKNOWN"
    assert run.status == "UNKNOWN"
    assert run.confirmation_state == ConfirmationState.RECONCILIATION_PENDING
    assert transport.submit_call_count == 1
    # 确认异常详情与 ExecutionRun 中绝未泄露已物化的 Secret 明文
    assert "SuperSecretValue123!" not in str(exc_info.value.detail)
    assert "SuperSecretValue123!" not in str(run.state_events)

    # 第二步：即便再次调用 submit_job，因同一 stable_submit_id 已登记为 UNKNOWN，绝不发起第二次 HTTP 提交！
    ref_again = adapter.submit_job(
        run=run,
        hocon_template=hocon_tpl,
        secret_resolver=lambda k: "SuperSecretValue123!",
    )
    assert transport.submit_call_count == 1
    assert ref_again.submit_state == "UNKNOWN"

    # 第三步：调用 RunReconciler 按 stable_submit_id 对账 -> 找到 Zeta 上的 RUNNING 作业 -> 恢复至 RUNNING
    outcome = reconciler.reconcile(run)
    assert outcome.found_on_engine is True
    assert outcome.external_job_id == "zeta-9900-1"
    assert outcome.resolved_status == "RUNNING"
    assert outcome.confirmation_state == ConfirmationState.ENGINE_CONFIRMED
    assert run.status == "RUNNING"
    assert run.external_job_id == "zeta-9900-1"
    assert run.confirmation_state == ConfirmationState.ENGINE_CONFIRMED
    assert transport.submit_call_count == 1


class _InMemoryControlPlaneRepo:
    """模拟 PostgreSQL 控制面持久化事实与 `resolve_execution_tenant_context` 的测试仓储。"""

    def __init__(self, run: ExecutionRunRecord, fact: AuthorizedCommandFact) -> None:
        self.run = run
        self.fact = fact
        self.rls_context_log: list[tuple[str, str, str]] = []

    def resolve_execution_tenant_context(
        self, execution_id: str, command_id: str
    ) -> tuple[str, str] | None:
        if execution_id == self.run.execution_id and command_id == self.fact.command_id:
            return (self.run.organization_id, self.run.project_id)
        return None

    def set_transaction_rls_context(
        self, *, organization_id: str, project_id: str, actor_id: str
    ) -> None:
        self.rls_context_log.append((organization_id, project_id, actor_id))

    def get_authorized_command_fact(
        self, command_id: str, execution_id: str
    ) -> AuthorizedCommandFact | None:
        if command_id == self.fact.command_id and execution_id == self.fact.execution_id:
            return self.fact
        return None

    def get_execution_run(self, execution_id: str) -> ExecutionRunRecord | None:
        return self.run if execution_id == self.run.execution_id else None

    def mark_command_completed(
        self, command_id: str, result_summary: dict[str, Any]
    ) -> None:
        if command_id == self.fact.command_id:
            self.fact.status = "COMPLETED"


def test_worker_dispatcher_resolves_rls_from_pg_and_handles_duplicate_command_idempotently() -> None:
    """验证 Worker 严格从 PG 派生 RLS 租户上下文（拒绝伪造 tenant_id 字段）且重复 `command_id` 幂等无副作用。"""
    transport = _FaultInjectableZetaTransport(timeout_on_submit=False)
    adapter = SeaTunnelZetaAdapter(transport=transport)

    exec_id = "11111111-2222-3333-4444-555555555555"
    cmd_id = "aaaaaaaa-1111-2222-3333-444444444444"
    jti = "99999999-1111-2222-3333-444444444444"

    run = ExecutionRunRecord(
        execution_id=exec_id,
        organization_id="org-trusted-pg",
        project_id="proj-trusted-pg",
        pipeline_id="pipe-1",
        pipeline_version_id="pv-1",
        artifact_hash="sha256:abc",
        execution_mode=ExecutionMode.BATCH,
        preparation_id="prep-1",
        capability_jti=jti,
        command_id=cmd_id,
    )
    fact = AuthorizedCommandFact(
        command_id=cmd_id,
        execution_id=exec_id,
        organization_id="org-trusted-pg",
        project_id="proj-trusted-pg",
        command_type="EXECUTION_SUBMIT_BATCH",
        capability_jti=jti,
        capability_consumed=True,
        payload_json={
            "hocon_template": 'env { job.mode = "BATCH" }\nsource { Jdbc { password = "${SECRET:MYSQL_PW}" } }'
        },
    )
    repo = _InMemoryControlPlaneRepo(run, fact)
    dispatcher = DataPlaneWorkerDispatcher(
        repository=repo,
        seatunnel_adapter=adapter,
    )

    valid_outbox_payload = {
        "schema_version": "1.0.0",
        "event_type": "EXECUTION_SUBMIT_BATCH",
        "command_id": cmd_id,
        "execution_id": exec_id,
        "capability_jti": jti,
        "capability_jws": "eyJhbGciOiJFZERTQSJ9.eyJqdGkiOiI5OTk5In0.signature_bytes_placeholder",
        "trace_id": "trace-idempotent-001",
        "created_at": "2026-11-22T14:20:00Z",
    }

    # 1. 若消息负载试图夹带伪造的 tenant_id 字段，契约校验直接拒绝
    tampered_payload = {**valid_outbox_payload, "tenant_id": "malicious-org"}
    with pytest.raises(ValueError, match="额外字段"):
        dispatcher.handle_outbox_command(tampered_payload)
    assert transport.submit_call_count == 0

    # 2. 首次合法投递：从 PG 派生 (org-trusted-pg, proj-trusted-pg) 注入 RLS，并向 Zeta 提交一次作业
    first_result = dispatcher.handle_outbox_command(valid_outbox_payload)
    assert first_result["status"] == "SUBMITTED"
    assert first_result["external_job_id"] == "zeta-9900-1"
    assert transport.submit_call_count == 1
    assert repo.rls_context_log[0][:2] == ("org-trusted-pg", "proj-trusted-pg")

    # 3. Celery At-least-once 重复投递同一 command_id：严格返回 IDEMPOTENT_NOOP，绝不产生第二次提交
    second_result = dispatcher.handle_outbox_command(valid_outbox_payload)
    assert second_result["status"] == "IDEMPOTENT_NOOP"
    assert second_result["side_effects_executed"] is False
    assert transport.submit_call_count == 1


# =============================================================================
# 测试 5：RollbackExecutor CDC C+D 恢复策略与批处理 SHADOW_CLEANUP / SWAP_RESTORE 测试
# =============================================================================


def test_rollback_executor_cdc_c_plus_d_recovery_policy() -> None:
    """验证 CDC C+D 恢复策略：
    - 有效历史检查点（Policy C）-> 直接恢复至 `STREAMING`；
    - `CHECKPOINT_LOST` 或 `POSITION_EXPIRED` 且无 Operator 显式确认（Policy D）-> 阻断并抛出 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`；
    - 附带 Operator 显式确认与理由 -> 允许进入 `RECOVERING -> INITIAL_SNAPSHOT` 重新快照。
    """
    executor = RollbackExecutor()

    # 1. 策略 C：从 DEGRADED 状态使用有效历史检查点恢复至 STREAMING
    cdc_run_valid_cp = ExecutionRunRecord(
        execution_id="cdc-run-valid-cp",
        organization_id="org-1",
        project_id="proj-1",
        pipeline_id="pipe-cdc",
        pipeline_version_id="pv-cdc-1",
        artifact_hash="sha256:cdc1",
        execution_mode=ExecutionMode.CDC,
        preparation_id="prep-cdc-1",
        capability_jti="jti-cdc-1",
        command_id="cmd-cdc-1",
        status="DEGRADED",
        checkpoint_ref="s3://etl-checkpoints/cdc-run-valid-cp/chk-42",
    )
    ctx_c = RollbackCommandContext(
        organization_id="org-1",
        project_id="proj-1",
        preparation_id="prep-rb-c",
        capability_jti="jti-rb-c",
        command_id="cmd-rb-c",
        initiated_by="operator-bob",
    )
    rb_c = executor.execute_cdc_recover(
        ctx=ctx_c,
        run=cdc_run_valid_cp,
        checkpoint_valid=True,
        position_expired=False,
        replication_slot_active=True,
    )
    assert rb_c.status == "SUCCEEDED"
    assert rb_c.result_json["recovery_policy"] == "POLICY_C_HISTORICAL_CHECKPOINT"
    assert rb_c.result_json["resnapshot_triggered"] is False
    assert cdc_run_valid_cp.status == "STREAMING"

    # 2. 策略 D 阻断测试：CHECKPOINT_LOST 或 POSITION_EXPIRED 且未获 Operator 显式确认 -> 必须阻断！
    for fault_status in ("CHECKPOINT_LOST", "POSITION_EXPIRED"):
        cdc_run_fault = ExecutionRunRecord(
            execution_id=f"cdc-run-{fault_status.lower()}",
            organization_id="org-1",
            project_id="proj-1",
            pipeline_id="pipe-cdc",
            pipeline_version_id="pv-cdc-1",
            artifact_hash="sha256:cdc1",
            execution_mode=ExecutionMode.CDC,
            preparation_id="prep-cdc-2",
            capability_jti="jti-cdc-2",
            command_id=f"cmd-{fault_status.lower()}",
            status=fault_status,
            checkpoint_ref="s3://etl-checkpoints/expired/chk-01",
        )
        ctx_d_unconfirmed = RollbackCommandContext(
            organization_id="org-1",
            project_id="proj-1",
            preparation_id=f"prep-unconfirmed-{fault_status}",
            capability_jti=f"jti-unconfirmed-{fault_status}",
            command_id=f"cmd-unconfirmed-{fault_status}",
            initiated_by="operator-bob",
        )
        with pytest.raises(CdcRecoveryOperatorConfirmRequiredError) as exc_info:
            executor.execute_cdc_recover(
                ctx=ctx_d_unconfirmed,
                run=cdc_run_fault,
                checkpoint_valid=(fault_status != "CHECKPOINT_LOST"),
                position_expired=(fault_status == "POSITION_EXPIRED"),
                operator_confirmed_resnapshot=False,
            )
        assert exc_info.value.error_code == "CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED"
        # 状态必须保持原故障态，绝不静默跃迁至 INITIAL_SNAPSHOT
        assert cdc_run_fault.status == fault_status

    # 3. 策略 D 显式确认恢复测试：Operator 显式确认并附带理由后，允许进入 INITIAL_SNAPSHOT
    cdc_run_confirmed = ExecutionRunRecord(
        execution_id="cdc-run-confirmed",
        organization_id="org-1",
        project_id="proj-1",
        pipeline_id="pipe-cdc",
        pipeline_version_id="pv-cdc-1",
        artifact_hash="sha256:cdc1",
        execution_mode=ExecutionMode.CDC,
        preparation_id="prep-cdc-3",
        capability_jti="jti-cdc-3",
        command_id="cmd-cdc-3",
        status="POSITION_EXPIRED",
        checkpoint_ref="s3://etl-checkpoints/expired/chk-02",
    )
    ctx_d_confirmed = RollbackCommandContext(
        organization_id="org-1",
        project_id="proj-1",
        preparation_id="prep-confirmed-d",
        capability_jti="jti-confirmed-d",
        command_id="cmd-confirmed-d",
        initiated_by="operator-alice",
    )
    rb_d = executor.execute_cdc_recover(
        ctx=ctx_d_confirmed,
        run=cdc_run_confirmed,
        checkpoint_valid=False,
        position_expired=True,
        operator_confirmed_resnapshot=True,
        operator_confirmation_reason="Approved full re-snapshot after MySQL binlog purge window",
    )
    assert rb_d.status == "SUCCEEDED"
    assert rb_d.result_json["recovery_policy"] == "POLICY_D_OPERATOR_CONFIRMED_RESNAPSHOT"
    assert rb_d.result_json["resnapshot_triggered"] is True
    assert cdc_run_confirmed.status == "INITIAL_SNAPSHOT"


def test_rollback_executor_shadow_cleanup_and_swap_restore() -> None:
    """验证未发布影子表清理（`SHADOW_CLEANUP`）与已发布版本原子恢复（`SWAP_RESTORE`）。"""
    sql_log: list[str] = []
    doris_adapter = DorisPublishAdapter(sql_executor=lambda sql: sql_log.append(sql) or {})
    executor = RollbackExecutor(
        doris_adapter=doris_adapter,
        sql_executor=lambda sql: sql_log.append(sql) or {},
    )

    # 1. SHADOW_CLEANUP：将 INTERRUPTED 批处理作业的未发布影子表清理并迁移至 ROLLED_BACK
    interrupted_run = ExecutionRunRecord(
        execution_id="exec-interrupted-01",
        organization_id="org-1",
        project_id="proj-1",
        pipeline_id="pipe-1",
        pipeline_version_id="pv-1",
        artifact_hash="sha256:111",
        execution_mode=ExecutionMode.BATCH,
        preparation_id="prep-1",
        capability_jti="jti-1",
        command_id="cmd-1",
        status="INTERRUPTED",
    )
    cleanup_ctx = RollbackCommandContext(
        organization_id="org-1",
        project_id="proj-1",
        preparation_id="prep-cleanup-1",
        capability_jti="jti-cleanup-1",
        command_id="cmd-cleanup-1",
        initiated_by="operator-1",
    )
    rb_cleanup = executor.execute_shadow_cleanup(
        ctx=cleanup_ctx,
        run=interrupted_run,
        shadow_table_name="demo_dw.dwd_order_user_wide__shadow_exec001",
    )
    assert rb_cleanup.rollback_type == RollbackType.SHADOW_CLEANUP
    assert interrupted_run.status == "ROLLED_BACK"
    assert sql_log[-1] == "DROP TABLE IF EXISTS demo_dw.dwd_order_user_wide__shadow_exec001"

    # 2. SWAP_RESTORE：将已发布的 SUCCEEDED 作业通过 archive_table_ref 原子替换恢复至旧版本
    target_schema = _sample_target_schema()
    archive_schema = _sample_compatible_shadow_schema("exec002")
    published_ver = doris_adapter.publish_shadow_to_target(
        organization_id="org-1",
        project_id="proj-1",
        execution_id="exec002",
        pipeline_version_id="pv-2",
        artifact_hash="sha256:222",
        target_connection_id="conn-doris",
        quality_report_id="qr-2",
        published_by="operator-1",
        target_schema=target_schema,
        shadow_schema=archive_schema,
        previous_version_id="pdv-001",
    )

    succeeded_run = ExecutionRunRecord(
        execution_id="exec002",
        organization_id="org-1",
        project_id="proj-1",
        pipeline_id="pipe-1",
        pipeline_version_id="pv-2",
        artifact_hash="sha256:222",
        execution_mode=ExecutionMode.BATCH,
        preparation_id="prep-2",
        capability_jti="jti-2",
        command_id="cmd-2",
        status="SUCCEEDED",
        published_dataset_version_id=published_ver.id,
    )
    restore_ctx = RollbackCommandContext(
        organization_id="org-1",
        project_id="proj-1",
        preparation_id="prep-restore-2",
        capability_jti="jti-restore-2",
        command_id="cmd-restore-2",
        initiated_by="operator-1",
    )
    rb_restore = executor.execute_swap_restore(
        ctx=restore_ctx,
        run=succeeded_run,
        published_version=published_ver,
        target_schema=target_schema,
        archive_schema=archive_schema,
    )
    assert rb_restore.rollback_type == RollbackType.SWAP_RESTORE
    assert succeeded_run.status == "ROLLED_BACK"
    assert rb_restore.result_json["restore_sql"] == (
        "ALTER TABLE demo_dw.dwd_order_user_wide "
        "REPLACE WITH TABLE dwd_order_user_wide__shadow_exec002 "
        "PROPERTIES('swap'='true')"
    )
