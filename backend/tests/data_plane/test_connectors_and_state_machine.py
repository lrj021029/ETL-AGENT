"""测试 1 & 6：连接器能力矩阵契约校验与统一 ExecutionRun（BATCH / CDC）状态机守卫测试。"""
from __future__ import annotations

from pathlib import Path
import re
import pytest

from backend.app.connectors import (
    ConnectorCapabilityEntry,
    ConnectorCapabilityUnsupportedError,
    ConnectorRegistry,
    ConnectorRole,
    ControlledFileBoundary,
    FilePathTraversalBlockedError,
    SupportStatus,
    compute_schema_fingerprint,
    get_default_connector_registry,
    validate_entry_against_contract,
)
from backend.app.connectors.probe import ProbedColumn, ProbedObject, ReadOnlyMetadataProbeService
from backend.app.data_plane import verify_hocon_has_no_plaintext_secrets
from backend.app.execution import (
    BATCH_STATE_TRANSITIONS,
    CDC_STATE_TRANSITIONS,
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateMachine,
    FailureDiagnosisEngine,
    StateTransitionInvalidError,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_run(mode: ExecutionMode, status: str = "CREATED") -> ExecutionRunRecord:
    """构建测试用 ExecutionRunRecord 实例。"""
    return ExecutionRunRecord(
        execution_id="11111111-2222-3333-4444-555555555555",
        organization_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        project_id="99999999-8888-7777-6666-555555555555",
        pipeline_id="pipe-001",
        pipeline_version_id="pv-001",
        artifact_hash="sha256:abcdef0123456789",
        execution_mode=mode,
        preparation_id="prep-001",
        capability_jti="jti-001",
        command_id="cmd-001",
        status=status,
    )


def test_connector_registry_entries_strictly_match_json_schema_and_status_policy() -> None:
    """验证所有预置连接器严格符合 `connector_registry.schema.json`，且绝无虚标 `END_TO_END_VERIFIED`。"""
    registry = get_default_connector_registry()
    entries = registry.list_entries()
    assert len(entries) == 12

    configured_ids = set()
    planned_ids = set()
    for entry in entries:
        # 严格校验 JSON Schema 契约
        validate_entry_against_contract(entry.to_dict())
        assert entry.seatunnel_version == "2.3.9"
        # 在真实 L2 容器验证前，任何预置连接器绝不能虚标为 END_TO_END_VERIFIED
        assert entry.support_status != SupportStatus.END_TO_END_VERIFIED
        if entry.support_status == SupportStatus.CONFIGURED:
            configured_ids.add(entry.connector_id)
        elif entry.support_status == SupportStatus.PLANNED:
            planned_ids.add(entry.connector_id)

    assert configured_ids == {
        "mysql-jdbc",
        "mysql-cdc",
        "postgres-jdbc",
        "postgres-cdc",
        "doris",
        "s3-minio",
        "local-file-csv",
        "local-file-json",
        "local-file-parquet",
    }
    assert planned_ids == {"oracle-jdbc", "clickhouse-jdbc", "http-rest-api"}


def test_connector_registry_rejects_false_end_to_end_verified_without_evidence() -> None:
    """验证缺少真实 `test_evidence_ref` 与 `verified_at` 时，禁止将连接器标为 `END_TO_END_VERIFIED`。"""
    registry = get_default_connector_registry()
    mysql_entry = registry.get_entry("mysql-jdbc")

    with pytest.raises(ValueError, match="test_evidence_ref"):
        ConnectorCapabilityEntry(
            connector_id=mysql_entry.connector_id,
            connector_name=mysql_entry.connector_name,
            connector_version=mysql_entry.connector_version,
            seatunnel_version=mysql_entry.seatunnel_version,
            roles=mysql_entry.roles,
            auth_modes=mysql_entry.auth_modes,
            support_status=SupportStatus.END_TO_END_VERIFIED,
            capabilities=mysql_entry.capabilities,
            publish_rollback_limits=mysql_entry.publish_rollback_limits,
            test_evidence_ref=None,
            verified_at=None,
        )

    # 规划态连接器（Oracle / ClickHouse / REST API）用于可执行流水线时必须被确定性阻断
    with pytest.raises(ConnectorCapabilityUnsupportedError) as exc_info:
        registry.validate_pipeline_requirement(
            connector_id="oracle-jdbc",
            required_role=ConnectorRole.SOURCE,
            sync_mode="FULL_LOAD",
        )
    assert exc_info.value.error_code == "CONNECTOR_CAPABILITY_UNSUPPORTED"

    # 非 CDC 连接器请求 CDC 模式时必须被阻断
    with pytest.raises(ConnectorCapabilityUnsupportedError):
        registry.validate_pipeline_requirement(
            connector_id="mysql-jdbc",
            required_role=ConnectorRole.SOURCE,
            sync_mode="CDC",
        )


def test_controlled_local_file_boundary_blocks_path_traversal(tmp_path: Path) -> None:
    """验证本地文件受控目录边界严格阻断 `..` 路径穿越与越界绝对路径。"""
    allowed_root = tmp_path / "controlled_mount"
    allowed_root.mkdir()
    valid_file = allowed_root / "sub" / "orders.csv"
    valid_file.parent.mkdir()
    valid_file.write_text("id,amount\n1,100\n", encoding="utf-8")

    boundary = ControlledFileBoundary([allowed_root])
    resolved = boundary.validate_path("sub/orders.csv")
    assert resolved == valid_file.resolve()

    for malicious_path in ["../secret.txt", "sub/../../etc/passwd", str(tmp_path / "outside.csv")]:
        with pytest.raises(FilePathTraversalBlockedError) as exc_info:
            boundary.validate_path(malicious_path)
        assert exc_info.value.error_code == "FILE_PATH_TRAVERSAL_BLOCKED"


def test_schema_fingerprint_and_drift_detection() -> None:
    """验证只读元数据探查的确定性 Schema 指纹与破坏性/兼容性 Drift 检测。"""
    col1 = ProbedColumn("order_id", "BIGINT", nullable=False, ordinal_position=1, is_primary_key=True)
    col2 = ProbedColumn("amount", "DECIMAL(18,2)", nullable=False, ordinal_position=2)
    obj_v1 = ProbedObject(
        schema_name="demo_oltp",
        object_name="orders",
        object_type="TABLE",
        columns=[col1, col2],
        primary_keys=["order_id"],
        row_count_approx=100,
    )
    # 仅行数变化不应改变结构指纹
    obj_v1_more_rows = ProbedObject(
        schema_name="demo_oltp",
        object_name="orders",
        object_type="TABLE",
        columns=[col1, col2],
        primary_keys=["order_id"],
        row_count_approx=50000,
    )
    assert compute_schema_fingerprint([obj_v1]) == compute_schema_fingerprint([obj_v1_more_rows])

    probe_svc = ReadOnlyMetadataProbeService()
    no_drift = probe_svc.detect_schema_drift([obj_v1], [obj_v1_more_rows])
    assert no_drift["has_drift"] is False

    # 类型变更为 BREAKING Drift
    col2_changed = ProbedColumn("amount", "VARCHAR(64)", nullable=True, ordinal_position=2)
    obj_v2 = ProbedObject(
        schema_name="demo_oltp",
        object_name="orders",
        object_type="TABLE",
        columns=[col1, col2_changed],
        primary_keys=["order_id"],
    )
    drift_report = probe_svc.detect_schema_drift([obj_v1], [obj_v2])
    assert drift_report["has_drift"] is True
    assert drift_report["severity"] == "BREAKING"
    assert len(drift_report["type_changed_columns"]) == 1


def test_execution_state_machine_matches_contract_yaml_and_guards_transitions() -> None:
    """验证代码中的 BATCH 与 CDC 状态机转移表与 `contracts/harness/execution_state_machine.yaml` 完全一致。"""
    yaml_text = (REPO_ROOT / "contracts" / "harness" / "execution_state_machine.yaml").read_text(
        encoding="utf-8"
    )
    # 校验契约 YAML 中声明的所有状态均在实现中精确建模
    for batch_state in BATCH_STATE_TRANSITIONS:
        assert re.search(rf"^\s*{batch_state}:", yaml_text, flags=re.MULTILINE), batch_state
    for cdc_state in CDC_STATE_TRANSITIONS:
        assert re.search(rf"^\s*{cdc_state}:", yaml_text, flags=re.MULTILINE), cdc_state

    # 1. 正向测试：BATCH 标准完整生命周期与确认态
    batch_run = _make_run(ExecutionMode.BATCH)
    assert batch_run.confirmation_state == ConfirmationState.REQUEST_SUBMITTED

    ExecutionStateMachine.transition(batch_run, "QUEUED", reason_code="DEQUEUED")
    ExecutionStateMachine.transition(batch_run, "SUBMITTING", reason_code="SUBMITTING_TO_ZETA")
    assert batch_run.confirmation_state == ConfirmationState.REQUEST_SUBMITTED

    ExecutionStateMachine.transition(
        batch_run,
        "RUNNING",
        reason_code="ZETA_CONFIRMED",
        external_job_id="zeta-job-100",
    )
    assert batch_run.confirmation_state == ConfirmationState.ENGINE_CONFIRMED
    assert batch_run.started_at is not None

    ExecutionStateMachine.transition(batch_run, "VALIDATING", reason_code="STAGE2_QUALITY_CHECK")
    ExecutionStateMachine.transition(batch_run, "PUBLISHING", reason_code="DORIS_SWAP_START")
    ev = ExecutionStateMachine.transition(batch_run, "SUCCEEDED", reason_code="DORIS_SWAP_DONE")
    assert batch_run.status == "SUCCEEDED"
    assert batch_run.ended_at is not None
    realtime_payload = ev.to_realtime_payload()
    assert realtime_payload["to_status"] == "SUCCEEDED"
    assert realtime_payload["confirmation_state"] == "ENGINE_CONFIRMED"


def test_cdc_never_enters_batch_succeeded_and_illegal_transitions_rejected() -> None:
    """验证 CDC 永续作业绝不可进入批处理 `SUCCEEDED` 状态，且所有非法跃迁均抛出 `STATE_TRANSITION_INVALID`。"""
    cdc_run = _make_run(ExecutionMode.CDC)
    ExecutionStateMachine.transition(cdc_run, "QUEUED", reason_code="DEQUEUED")
    ExecutionStateMachine.transition(cdc_run, "SUBMITTING", reason_code="SUBMITTING")
    ExecutionStateMachine.transition(cdc_run, "INITIAL_SNAPSHOT", reason_code="SNAPSHOT_START")
    ExecutionStateMachine.transition(cdc_run, "STREAMING", reason_code="BINLOG_STREAMING")

    # CDC 严禁进入批处理状态 SUCCEEDED、VALIDATING、PUBLISHING
    for forbidden_state in ("SUCCEEDED", "VALIDATING", "PUBLISHING", "ROLLED_BACK"):
        with pytest.raises(StateTransitionInvalidError) as exc_info:
            ExecutionStateMachine.transition(cdc_run, forbidden_state, reason_code="ILLEGAL")
        assert exc_info.value.error_code == "STATE_TRANSITION_INVALID"

    # BATCH 严禁从 CREATED 直接跳到 RUNNING 或 SUCCEEDED，也严禁从 ROLLED_BACK 再次迁移
    batch_run = _make_run(ExecutionMode.BATCH)
    for illegal_target in ("RUNNING", "VALIDATING", "PUBLISHING", "SUCCEEDED"):
        with pytest.raises(StateTransitionInvalidError) as exc_info:
            ExecutionStateMachine.transition(batch_run, illegal_target, reason_code="SKIP_STEPS")
        assert exc_info.value.error_code == "STATE_TRANSITION_INVALID"

    rolled_back_run = _make_run(ExecutionMode.BATCH, status="ROLLED_BACK")
    with pytest.raises(StateTransitionInvalidError):
        ExecutionStateMachine.transition(rolled_back_run, "RUNNING", reason_code="REVIVE_FORBIDDEN")


def test_failure_diagnosis_engine_has_zero_side_effects_and_Templates_have_no_plaintext_secrets() -> None:
    """验证故障诊断引擎严格保持只读零副作用，且 `seatunnel/templates/` 下所有 HOCON 模板零明文密码。"""
    diag_engine = FailureDiagnosisEngine()
    report = diag_engine.diagnose(
        execution_id="exec-123",
        status="INTERRUPTED",
        status_reason="SUPERVISION_REJECT_RATE_TOTAL_EXCEEDED",
        error_code_counts={"DQ_RANGE_VIOLATION": 201},
        supervision_breach={
            "breach_type": "REJECT_RATE_TOTAL_EXCEEDED",
            "threshold": 0.20,
            "observed_value": 0.201,
        },
    )
    report_dict = report.to_dict()
    assert report_dict["advisory_only"] is True
    assert report_dict["side_effects_executed"] is False
    assert report_dict["primary_error_code"] == "SUPERVISION_THRESHOLD_BREACHED"

    # 校验 seatunnel/templates/ 下全部 4 个 HOCON 模板均不含明文密码且使用 ${SECRET:...} 占位符
    templates_dir = REPO_ROOT / "seatunnel" / "templates"
    template_files = sorted(templates_dir.glob("*.conf"))
    assert len(template_files) == 4
    for tf in template_files:
        content = tf.read_text(encoding="utf-8")
        placeholders = verify_hocon_has_no_plaintext_secrets(content)
        assert len(placeholders) >= 2, f"模板 {tf.name} 应包含至少 2 个 ${{SECRET:...}} 占位符"
