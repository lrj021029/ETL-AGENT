"""测试 2：两阶段质量契约（QualityContract）与运行时监督（RuntimeSupervisionEngine）边界测试。

重点覆盖用户确认的核心不变量（DEC-006、DEC-012、ADR-004）：
- `reject_rate = error_rows / validated_rows`
- `error_rows=200, validated_rows=1000` (`reject_rate == 0.20`) -> 绝不触发整体拒绝率超限！
- `error_rows=201, validated_rows=1000` (`reject_rate == 0.201 > 0.20`) -> 必须触发超限并执行 `HARD_INTERRUPT`！
- 滚动窗口超限、绝对错误行数超限、`min_validated_rows` 最小样本门控、以及读行数/写字节/时长/放大比/CDC 延迟预算超限。
"""
from __future__ import annotations

from typing import Any
import pytest

from backend.app.execution import ExecutionMode, ExecutionRunRecord, ExecutionStateMachine
from backend.app.quality import (
    QualitySeverity,
    QualityStage,
    RawValueStoragePolicy,
    RestrictedSqlAstValidator,
    SqlAstPolicyViolationError,
    TwoStageQualityEngine,
    redact_raw_payload,
    validate_quality_contract_schema,
)
from backend.app.supervision import (
    ExecutionMetricsSnapshot,
    RuntimeSupervisionContract,
    RuntimeSupervisionEngine,
    WindowDefinition,
    WindowMode,
    compute_reject_rate,
    validate_runtime_supervision_schema,
)


class _SpyCancelAdapter:
    """记录受管取消调用的测试替身适配器。"""

    def __init__(self) -> None:
        self.cancelled_job_ids: list[str] = []

    def cancel_job(self, external_job_id: str) -> dict[str, Any]:
        self.cancelled_job_ids.append(external_job_id)
        return {"jobId": external_job_id, "cancelled": True}


def _default_supervision_contract(**overrides: Any) -> RuntimeSupervisionContract:
    """构建符合 `runtime_supervision.schema.json` 的测试契约实例。"""
    base: dict[str, Any] = {
        "schema_version": "1.0.0",
        "max_read_rows": 100000,
        "max_write_bytes": 104857600,
        "max_duration_seconds": 600,
        "max_amplification_ratio": 2.0,
        "max_reject_rate_total": 0.20,
        "max_reject_rate_window": 0.25,
        "window_definition": {"mode": "ROW_COUNT", "size": 500},
        "min_validated_rows": 100,
        "max_error_rows_absolute": 5000,
        "action_on_breach": "HARD_INTERRUPT",
        "cdc_max_lag_seconds": 30,
    }
    base.update(overrides)
    return validate_runtime_supervision_schema(base)


def test_reject_rate_exact_boundary_200_vs_201_on_1000_rows() -> None:
    """强制边界测试：`200/1000 == 0.20` 绝不触发超限；`201/1000 == 0.201 > 0.20` 必须触发 `HARD_INTERRUPT`。"""
    contract = _default_supervision_contract()
    engine = RuntimeSupervisionEngine()

    # 1. error_rows = 200, validated_rows = 1000 -> reject_rate == 0.20 -> MUST NOT BREACH!
    metrics_at_20_pct = ExecutionMetricsSnapshot(
        execution_id="exec-boundary-200",
        read_rows=1000,
        validated_rows=1000,
        valid_rows=800,
        write_rows=800,
        write_bytes=65536,
        error_rows=200,
        duration_seconds=12.0,
    )
    assert compute_reject_rate(200, 1000) == 0.20
    eval_200 = engine.evaluate(contract, metrics_at_20_pct)
    assert eval_200.breached is False
    assert eval_200.action_to_take == "NONE"
    assert eval_200.breaches == []

    # 2. error_rows = 201, validated_rows = 1000 -> reject_rate == 0.201 > 0.20 -> MUST BREACH!
    metrics_at_20_1_pct = ExecutionMetricsSnapshot(
        execution_id="exec-boundary-201",
        read_rows=1000,
        validated_rows=1000,
        valid_rows=799,
        write_rows=799,
        write_bytes=65536,
        error_rows=201,
        duration_seconds=12.5,
    )
    eval_201 = engine.evaluate(contract, metrics_at_20_1_pct)
    assert eval_201.breached is True
    assert eval_201.action_to_take == "HARD_INTERRUPT"
    assert len(eval_201.breaches) == 1
    assert eval_201.breaches[0].breach_type == "REJECT_RATE_TOTAL_EXCEEDED"
    assert eval_201.breaches[0].threshold == 0.20
    assert eval_201.breaches[0].observed_value == pytest.approx(0.201)

    # 验证触发 HARD_INTERRUPT 时通过受管 Adapter 停止作业，将状态置为 INTERRUPTED，且绝不自动回滚
    run = ExecutionRunRecord(
        execution_id="exec-boundary-201",
        organization_id="org-1",
        project_id="proj-1",
        pipeline_id="pipe-1",
        pipeline_version_id="pv-1",
        artifact_hash="sha256:123",
        execution_mode=ExecutionMode.BATCH,
        preparation_id="prep-1",
        capability_jti="jti-1",
        command_id="cmd-1",
        status="CREATED",
    )
    ExecutionStateMachine.transition(run, "QUEUED", reason_code="Q")
    ExecutionStateMachine.transition(run, "SUBMITTING", reason_code="S")
    ExecutionStateMachine.transition(
        run, "RUNNING", reason_code="R", external_job_id="zeta-job-201"
    )

    spy_adapter = _SpyCancelAdapter()
    enforced = engine.supervise_and_enforce(
        run=run,
        contract=contract,
        metrics=metrics_at_20_1_pct,
        adapter=spy_adapter,
        error_code_counts={"DQ_RANGE_VIOLATION": 201},
    )
    assert enforced.breached is True
    assert spy_adapter.cancelled_job_ids == ["zeta-job-201"]
    assert run.status == "INTERRUPTED"
    assert run.diagnosis_json is not None
    assert run.diagnosis_json["advisory_only"] is True
    assert run.diagnosis_json["side_effects_executed"] is False


def test_supervision_min_validated_rows_window_absolute_and_budget_breaches() -> None:
    """验证最小样本量门控、滚动窗口拒绝率、绝对错误数上限以及四项资源预算（行数/字节/时长/放大比）与 CDC 延迟。"""
    contract = _default_supervision_contract(
        min_validated_rows=100,
        max_reject_rate_total=0.20,
        max_reject_rate_window=0.25,
        max_error_rows_absolute=500,
        max_read_rows=10000,
        max_write_bytes=102400,
        max_duration_seconds=60,
        max_amplification_ratio=1.5,
        cdc_max_lag_seconds=10,
    )
    engine = RuntimeSupervisionEngine()

    # 1. 样本量未达到 min_validated_rows (50 < 100) 时，即便 20/50 = 40% > 20% 也不触发比例阈值
    small_sample = ExecutionMetricsSnapshot(
        execution_id="exec-small",
        read_rows=50,
        validated_rows=50,
        valid_rows=30,
        write_rows=30,
        write_bytes=2048,
        error_rows=20,
        duration_seconds=2.0,
    )
    assert engine.evaluate(contract, small_sample).breached is False

    # 2. 滚动窗口拒绝率超限（整体 150/2000 = 7.5% <= 20%，但最近 500 行窗口内 130/500 = 26% > 25%）
    win_val, win_err = engine.record_incremental_sample(
        execution_id="exec-win",
        timestamp_epoch_sec=100.0,
        delta_validated_rows=500,
        delta_error_rows=130,
        window_def=WindowDefinition(mode=WindowMode.ROW_COUNT, size=500),
    )
    window_breach_metrics = ExecutionMetricsSnapshot(
        execution_id="exec-win",
        read_rows=2000,
        validated_rows=2000,
        valid_rows=1850,
        write_rows=1850,
        write_bytes=32768,
        error_rows=150,
        duration_seconds=15.0,
        window_validated_rows=win_val,
        window_error_rows=win_err,
    )
    res_win = engine.evaluate(contract, window_breach_metrics)
    assert res_win.breached is True
    assert [b.breach_type for b in res_win.breaches] == ["REJECT_RATE_WINDOW_EXCEEDED"]

    # 3. 绝对错误行数上限超限（整体 501/10000 = 5.01% <= 20%，但 error_rows=501 > 500）
    abs_breach_metrics = ExecutionMetricsSnapshot(
        execution_id="exec-abs",
        read_rows=10000,
        validated_rows=10000,
        valid_rows=9499,
        write_rows=9499,
        write_bytes=65536,
        error_rows=501,
        duration_seconds=20.0,
        window_validated_rows=500,
        window_error_rows=10,
    )
    res_abs = engine.evaluate(contract, abs_breach_metrics)
    assert res_abs.breached is True
    assert [b.breach_type for b in res_abs.breaches] == ["MAX_ERROR_ROWS_ABSOLUTE_EXCEEDED"]

    # 4. 资源预算与放大比、CDC Lag 同时超限
    budget_breach_metrics = ExecutionMetricsSnapshot(
        execution_id="exec-budget",
        read_rows=10001,
        validated_rows=10001,
        valid_rows=10001,
        write_rows=20002,  # amplification_ratio = 2.0 > 1.5
        write_bytes=204800,  # > 102400
        error_rows=0,
        duration_seconds=75.0,  # > 60
        cdc_lag_ms=15000,  # 15s > 10s
    )
    res_budget = engine.evaluate(contract, budget_breach_metrics)
    assert res_budget.breached is True
    breach_types = {b.breach_type for b in res_budget.breaches}
    assert breach_types == {
        "MAX_READ_ROWS_EXCEEDED",
        "MAX_WRITE_BYTES_EXCEEDED",
        "MAX_DURATION_SECONDS_EXCEEDED",
        "MAX_AMPLIFICATION_RATIO_EXCEEDED",
        "CDC_MAX_LAG_SECONDS_EXCEEDED",
    }


def test_two_stage_quality_engine_row_split_shadow_validation_and_redaction() -> None:
    """验证 ADR-004 两阶段 QualityContract：Stage 1 行级分流 + Stage 2 影子表 UNIQUE/受限 SQL + 敏感字段脱敏。"""
    contract_dict = {
        "schema_version": "1.0.0",
        "error_table_name": "dwd_orders__error",
        "raw_value_storage_policy": "REDACTED_BY_CLASSIFICATION",
        "rules": [
            {
                "rule_id": "r_not_null_order_id",
                "rule_type": "NOT_NULL",
                "stage": "STREAMING_ROW_SPLIT",
                "column_name": "order_id",
                "error_code": "DQ_NOT_NULL_VIOLATION",
                "severity": "REJECT_ROW",
                "params": {},
            },
            {
                "rule_id": "r_amount_range",
                "rule_type": "RANGE",
                "stage": "STREAMING_ROW_SPLIT",
                "column_name": "amount",
                "error_code": "DQ_RANGE_VIOLATION",
                "severity": "REJECT_ROW",
                "params": {"min": 0, "max": 10000},
            },
            {
                "rule_id": "r_status_enum",
                "rule_type": "ENUM",
                "stage": "STREAMING_ROW_SPLIT",
                "column_name": "status",
                "error_code": "DQ_ENUM_VIOLATION",
                "severity": "REJECT_ROW",
                "params": {"allowed_values": ["PAID", "SHIPPED", "COMPLETED"]},
            },
            {
                "rule_id": "r_unique_order_id",
                "rule_type": "UNIQUE",
                "stage": "SHADOW_TABLE_VALIDATION",
                "column_name": "order_id",
                "error_code": "DQ_UNIQUE_VIOLATION",
                "severity": "FAIL_RUN",
                "params": {"columns": ["order_id"]},
            },
            {
                "rule_id": "r_restricted_sql_check",
                "rule_type": "RESTRICTED_SQL",
                "stage": "SHADOW_TABLE_VALIDATION",
                "column_name": None,
                "error_code": "DQ_RESTRICTED_SQL_VIOLATION",
                "severity": "FAIL_RUN",
                "params": {},
                "sql_dialect": "doris",
                "sql_text": "SELECT COUNT(*) FROM dwd_orders__shadow_exec01 WHERE amount < 0",
            },
        ],
    }
    contract = validate_quality_contract_schema(contract_dict)
    engine = TwoStageQualityEngine()

    input_rows = [
        {"order_id": "O-100", "amount": 250.0, "status": "PAID", "customer_phone": "13812345678"},
        {"order_id": "O-101", "amount": 500.0, "status": "SHIPPED", "customer_phone": "13987654321"},
        # 重复主键 O-100，将通过 Stage 1 但在 Stage 2 SHADOW_TABLE_VALIDATION 被 UNIQUE 规则捕获
        {"order_id": "O-100", "amount": 300.0, "status": "PAID", "customer_phone": "13812345678"},
        # Stage 1 RANGE 违规（负数金额）
        {"order_id": "O-102", "amount": -50.0, "status": "PAID", "customer_phone": "13711112222"},
        # Stage 1 NOT_NULL 违规
        {"order_id": None, "amount": 100.0, "status": "PAID", "customer_phone": "13600009999"},
    ]

    stage1 = engine.run_stage1_row_split(
        execution_id="exec-01",
        contract=contract,
        rows=input_rows,
        sensitive_columns={"customer_phone"},
    )
    # 3 行进入影子表（含重复的 O-100），2 行在 Stage 1 分流至错误表
    assert len(stage1.shadow_table_rows) == 3
    assert len(stage1.error_table_records) == 2
    for err_rec in stage1.error_table_records:
        # 敏感手机号必须已被掩码脱敏，绝不保留完整明文
        assert "13711112222" not in str(err_rec.raw_payload_redacted)
        assert "13600009999" not in str(err_rec.raw_payload_redacted)
        assert "***" in str(err_rec.raw_payload_redacted["customer_phone"])

    # 验证 HASH_ONLY 与 OMIT_SENSITIVE 两种脱敏策略
    hashed = redact_raw_payload(
        {"customer_phone": "13812345678"}, RawValueStoragePolicy.HASH_ONLY
    )
    assert str(hashed["customer_phone"]).startswith("sha256:")
    omitted = redact_raw_payload(
        {"order_id": "O-1", "customer_phone": "13812345678"},
        RawValueStoragePolicy.OMIT_SENSITIVE,
        sensitive_columns={"customer_phone"},
    )
    assert "customer_phone" not in omitted

    # 执行 Stage 2 影子表质量校验
    report = engine.run_stage2_shadow_validation(
        organization_id="org-1",
        project_id="proj-1",
        execution_id="exec-01",
        contract_id="qc-01",
        shadow_table_name="dwd_orders__shadow_exec01",
        contract=contract,
        stage1_outcome=stage1,
        total_input_rows=len(input_rows),
        sensitive_columns={"customer_phone"},
        restricted_sql_executor=lambda sql: 0,
    )
    assert report.validated_rows == 5
    # Stage 1 有 2 行违规 + Stage 2 UNIQUE 发现 1 行重复 = 共 3 行错误
    assert report.error_rows == 3
    assert report.valid_rows == 2
    assert report.reject_rate == pytest.approx(3 / 5)
    assert report.passed is False
    assert any(f["error_code"] == "DQ_UNIQUE_VIOLATION" for f in report.failure_reasons_json)


def test_restricted_sql_ast_validator_blocks_non_select_and_unauthorized_tables() -> None:
    """验证 Stage 2 受限 SQL AST 校验器严格拦截 DDL/DML、多语句、未授权表与危险函数。"""
    validator = RestrictedSqlAstValidator()
    allowed = {"dwd_orders__shadow_exec01"}

    valid_res = validator.validate_select_sql(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM dwd_orders__shadow_exec01 WHERE amount < 0",
        allowed_tables=allowed,
    )
    assert valid_res["valid"] is True

    malicious_queries = [
        "DROP TABLE dwd_orders__shadow_exec01",
        "DELETE FROM dwd_orders__shadow_exec01 WHERE 1=1",
        "SELECT COUNT(*) FROM dwd_orders__shadow_exec01; DROP TABLE users",
        "SELECT COUNT(*) FROM other_tenant_secret_table",
        "SELECT SLEEP(10) FROM dwd_orders__shadow_exec01",
        "SELECT COUNT(*) FROM information_schema.tables",
        "SELECT COUNT(*) FROM dwd_orders__shadow_exec01 -- comment bypass",
    ]
    for bad_sql in malicious_queries:
        with pytest.raises(SqlAstPolicyViolationError) as exc_info:
            validator.validate_select_sql(bad_sql, allowed_tables=allowed)
        assert exc_info.value.error_code == "SQL_AST_POLICY_VIOLATION"
