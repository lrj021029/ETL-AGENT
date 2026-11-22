"""全模块端到端 FastAPI TestClient 集成验收测试套件（Full API Integration Wiring Suite）。

验证 Wave 1 全部 7 大模块通过 `backend/app/api/wiring.py`、`backend/app/main.py`、
`backend/app/api/routers.py` 与 `backend/workers/celery_app.py` 真实协同工作：
1. `/health` 与 `/metrics`（Prometheus 文本暴露格式）返回 200 且导出全部 12 项契约指标。
2. `/api/v1/connectors/registry` 返回真实版本化 Connector 能力矩阵（诚实标注 `CONFIGURED`/`PLANNED`，零虚假 `END_TO_END_VERIFIED`）。
3. 完整 IAM 闭环：预置 6 类演示账号 -> 密码登录 -> 邮件二次验证（Email OTP）-> JWT Access Token -> Org/Project 双重成员上下文 -> `/overview`。
4. 数据源连接 + 只读元数据快照（`MetadataSnapshot`）+ Schema Drift 差异计算（API 响应与数据库零明文密钥）。
5. LangGraph Agent 线程闭环：创建线程 -> 缺参调用触发 `INTERRUPTED` 澄清中断 -> 跨租户/跨用户 Resume 返回 403 `TENANT_ACCESS_DENIED` -> 合法 Owner Resume -> 产出 EtlPlan/SQL/HOCON -> 确定性门禁（`DeterministicGate`）校验通过 -> 冻结不可变 `PipelineVersion` + JCS Canonical SHA256 `ArtifactManifest`。
6. Harness 安全内核闭环：
   - `POST /preparations`（PDP 为 prod 环境分配 P1 双审批槽位 `DATA_CHECKER` + `SECURITY_CHECKER`）。
   - Maker 企图自批 -> 403 `APPROVAL_SELF_APPROVE_FORBIDDEN`。
   - Data Checker 审批槽位 1 -> 同一用户企图兼任槽位 2 -> 403 `APPROVAL_SLOT_CONFLICT`。
   - Security Checker 审批槽位 2 -> 双槽集齐，准备单状态晋升为 `APPROVED`。
   - Operator 携带 `Idempotency-Key` 调用 `POST /commit` -> 202 返回 `execution_id`、`command_id`，并在同一事务内原子写入 `CapabilityIssuance`、`CapabilityConsumption`、`ExecutionRun`、`ExecutionCommand`、`OutboxEvent`、`OutboxDelivery` 与 `EvidenceEvent`。
   - Evidence Ledger `POST /evidence/verify` -> `chain_valid: true, signatures_valid: true`。
   - 受管回滚 `POST /rollbacks/prepare` -> 强制走 Harness Preparation 流程（绝不直接执行 SQL）。
   - Outbox 与 Celery Worker 任务端到端消费并执行幂等防重投保护。
7. Benchmark 评测闭环：`POST /benchmarks/datasets` 生成固定随机种子确定性数据集 -> `POST /benchmarks/runs` 运行真实 L0/L1 套件并验证 P0/P1 独立一票否决硬门槛。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Generator
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.auth.service import seed_demo_iam_data  # noqa: E402
from app.connections.service import ConnectionService  # noqa: E402
from app.connectors.registry import validate_entry_against_contract  # noqa: E402
from app.main import create_app  # noqa: E402
from app.observability.telemetry import STANDARD_METRIC_NAMES  # noqa: E402
from app.shared.errors import ErrorCode  # noqa: E402
from app.shared.models import Base  # noqa: E402
from app.shared.models.evidence_benchmark import EvidenceEvent  # noqa: E402
from app.shared.models.governance_harness import (  # noqa: E402
    CapabilityConsumption,
    CapabilityIssuance,
    ExecutionCommand,
    OutboxDelivery,
    OutboxEvent,
)
from workers.celery_app import (  # noqa: E402
    celery_app,
    dispatch_pending_events,
    evaluate_due_schedules,
    execute_batch_command,
    reconcile_unknown_run,
)


@pytest.fixture()
def integration_db_factory() -> Generator[sessionmaker[Session], None, None]:
    """创建包含全量 71 张表的内存数据库会话工厂。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def seeded_iam(integration_db_factory: sessionmaker[Session]) -> dict[str, Any]:
    """初始化固定系统角色、权限、多组织、多项目及 6 类预置演示账号。"""
    session = integration_db_factory()
    try:
        seeded = seed_demo_iam_data(session)
        session.commit()
        return seeded
    finally:
        session.close()


@pytest.fixture()
def client(
    integration_db_factory: sessionmaker[Session],
    seeded_iam: dict[str, Any],
    tmp_path: Path,
) -> Generator[TestClient, None, None]:
    """提供完成全量模块装配（wire_application）的 FastAPI TestClient。"""
    app = create_app(session_factory=integration_db_factory)
    conn_svc = ConnectionService()
    conn_svc.settings.allowed_upload_root = tmp_path / "uploads"
    app.state.connection_service = conn_svc

    with TestClient(app) as tc:
        yield tc


def _login_user(client: TestClient, email: str, password: str = "DemoPass!2026") -> dict[str, Any]:
    """执行密码登录 + 邮件二次验证（Email OTP）并返回签发的 JWT Token 字典。"""
    login_resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 200, login_resp.text
    challenge_data = login_resp.json()
    assert challenge_data["verification_type"] == "EMAIL_OTP"
    assert challenge_data["verification_label"] == "邮件二次验证"

    otp_outbox = client.app.state.auth_service.sent_otp_outbox
    matching_otp = [r for r in otp_outbox if str(r.challenge_id) == challenge_data["challenge_id"]]
    assert matching_otp, "未在内存投递箱中捕获到邮件二次验证 OTP"

    verify_resp = client.post(
        "/api/v1/auth/otp/verify",
        json={"challenge_id": challenge_data["challenge_id"], "otp_code": matching_otp[-1].otp_code},
    )
    assert verify_resp.status_code == 200, verify_resp.text
    tokens = verify_resp.json()
    assert tokens["access_token"].startswith("eyJ")
    assert tokens["refresh_token"].startswith("eyJ")
    return tokens


def test_1_health_and_prometheus_metrics_endpoints(client: TestClient) -> None:
    """验收项 1：/health 与 /metrics（Prometheus 暴露格式）均返回 200 且包含契约规定的 12 项标准指标。"""
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert "X-Trace-Id" in health_resp.headers
    health_json = health_resp.json()
    assert health_json["status"] == "ok"
    assert health_json["dependencies"]["postgres"] == "ok"

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    metrics_text = metrics_resp.text
    for metric_name in STANDARD_METRIC_NAMES:
        assert metric_name in metrics_text, f"/metrics 缺少标准指标: {metric_name}"


def test_2_connector_registry_honest_capability_matrix(client: TestClient) -> None:
    """验收项 2：/api/v1/connectors/registry 返回真实版本化连接器能力矩阵，全部符合契约 Schema，零虚假 END_TO_END_VERIFIED。"""
    resp = client.get("/api/v1/connectors/registry")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 12

    statuses = {entry["connector_id"]: entry["support_status"] for entry in items}
    assert statuses["mysql-jdbc"] == "CONFIGURED"
    assert statuses["mysql-cdc"] == "CONFIGURED"
    assert statuses["postgres-jdbc"] == "CONFIGURED"
    assert statuses["postgres-cdc"] == "CONFIGURED"
    assert statuses["doris"] == "CONFIGURED"
    assert statuses["s3-minio"] == "CONFIGURED"
    assert statuses["oracle-jdbc"] == "PLANNED"
    assert statuses["clickhouse-jdbc"] == "PLANNED"
    assert statuses["http-rest-api"] == "PLANNED"

    for entry in items:
        validate_entry_against_contract(entry)
        assert entry["support_status"] != "END_TO_END_VERIFIED", (
            f"连接器 {entry['connector_id']} 在未附带真实 L2 验收证据前不得虚标为 END_TO_END_VERIFIED"
        )


def test_3_full_iam_flow_and_project_overview(
    client: TestClient,
    seeded_iam: dict[str, Any],
) -> None:
    """验收项 3：完整 IAM 流程（预置账号 -> 密码登录 -> Email OTP 验证 -> JWT -> 组织/项目上下文 -> /overview）。"""
    tokens = _login_user(client, "maker@etl-agent.local")
    auth_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    me_resp = client.get("/api/v1/auth/me", headers=auth_headers)
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["user"]["email"] == "maker@etl-agent.local"
    assert me_data["verification_label"] == "邮件二次验证"
    assert len(me_data["organizations"]) >= 2

    org_alpha_id = str(seeded_iam["organizations"]["org-alpha"].id)
    proj_prod_id = str(seeded_iam["projects"]["proj-orders-prod"].id)
    tenant_headers = {**auth_headers, "X-Organization-Id": org_alpha_id}

    projs_resp = client.get("/api/v1/projects", headers=tenant_headers)
    assert projs_resp.status_code == 200
    assert any(p["id"] == proj_prod_id for p in projs_resp.json()["items"])

    overview_resp = client.get(f"/api/v1/projects/{proj_prod_id}/overview", headers=tenant_headers)
    assert overview_resp.status_code == 200
    overview_data = overview_resp.json()
    assert overview_data["organization_id"] == org_alpha_id
    assert overview_data["project_id"] == proj_prod_id
    assert "connections" in overview_data
    assert "executions" in overview_data
    assert "governance_gates" in overview_data


def test_4_connection_metadata_snapshot_drift_and_zero_plaintext_secrets(
    client: TestClient,
    seeded_iam: dict[str, Any],
) -> None:
    """验收项 4：连接创建 + 元数据探查不可变快照 + Schema Drift 检测（响应与存储零明文凭据）。"""
    tokens = _login_user(client, "maker@etl-agent.local")
    org_id = str(seeded_iam["organizations"]["org-alpha"].id)
    proj_id = str(seeded_iam["projects"]["proj-orders-prod"].id)
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": org_id,
    }

    secret_pwd = "MyTopSecretProdDbPwd!2026"
    conn_resp = client.post(
        f"/api/v1/projects/{proj_id}/connections",
        headers=headers,
        json={
            "name": "orders-mysql-source",
            "connector_id": "mysql-jdbc",
            "connection_type": "MYSQL",
            "config": {
                "host": "127.0.0.1",
                "port": 3306,
                "database": "sales",
                "username": "etl_user",
                "password": secret_pwd,
                "jdbc_url": f"mysql://etl_user:{secret_pwd}@127.0.0.1:3306/sales",
            },
        },
    )
    assert conn_resp.status_code == 201
    assert secret_pwd not in conn_resp.text
    conn_id = conn_resp.json()["id"]

    # 第一次元数据探查 -> Snapshot v1
    v1_objects = [
        {
            "schema_name": "sales",
            "object_name": "orders",
            "object_type": "TABLE",
            "row_count_approx": 500,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "data_type": "BIGINT", "nullable": False},
                {"column_name": "customer_email", "data_type": "VARCHAR(128)", "nullable": False},
                {"column_name": "amount", "data_type": "DECIMAL(18,2)", "nullable": False},
            ],
            "sample_rows": [
                {"id": 1, "customer_email": "buyer@corp.example.com", "amount": 99.50},
            ],
        }
    ]
    snap1_resp = client.post(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/probe",
        headers=headers,
        json={"connector_version": "2.3.9", "override_objects": v1_objects},
    )
    assert snap1_resp.status_code == 201
    assert "buyer@corp.example.com" not in snap1_resp.text
    snap1 = snap1_resp.json()

    # 第二次元数据探查（发生破坏性列类型变更）-> Snapshot v2 + HIGH_BREAKING Drift
    v2_objects = [
        {
            "schema_name": "sales",
            "object_name": "orders",
            "object_type": "TABLE",
            "row_count_approx": 600,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "data_type": "BIGINT", "nullable": False},
                {"column_name": "customer_email", "data_type": "VARCHAR(128)", "nullable": False},
                {"column_name": "amount", "data_type": "VARCHAR(32)", "nullable": False},
            ],
            "sample_rows": [],
        }
    ]
    snap2_resp = client.post(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/probe",
        headers=headers,
        json={"connector_version": "2.3.9", "override_objects": v2_objects},
    )
    assert snap2_resp.status_code == 201
    snap2 = snap2_resp.json()
    assert snap2["snapshot_id"] != snap1["snapshot_id"]
    assert snap2["drift_event"]["severity"] == "HIGH_BREAKING"

    drift_resp = client.get(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/drift",
        headers=headers,
    )
    assert drift_resp.status_code == 200
    assert len(drift_resp.json()["items"]) == 1


def test_5_agent_thread_interrupt_cross_tenant_block_resume_gate_and_freeze(
    client: TestClient,
    seeded_iam: dict[str, Any],
) -> None:
    """验收项 5：Agent 线程创建 -> 缺参中断 -> 跨租户/跨用户恢复 403 -> 合法恢复产出候选 -> Gate 校验 -> 冻结不可变 PipelineVersion + Canonical SHA256 Manifest。"""
    maker_tokens = _login_user(client, "maker@etl-agent.local")
    auditor_tokens = _login_user(client, "auditor@etl-agent.local")

    org_alpha_id = str(seeded_iam["organizations"]["org-alpha"].id)
    org_beta_id = str(seeded_iam["organizations"]["org-beta"].id)
    proj_prod_id = str(seeded_iam["projects"]["proj-orders-prod"].id)
    proj_beta_id = str(seeded_iam["projects"]["proj-risk-dev"].id)

    maker_headers = {
        "Authorization": f"Bearer {maker_tokens['access_token']}",
        "X-Organization-Id": org_alpha_id,
    }

    # 1. 创建源端 MySQL 连接、目标 Doris 连接与元数据快照
    src_conn = client.post(
        f"/api/v1/projects/{proj_prod_id}/connections",
        headers=maker_headers,
        json={
            "name": "agent-src-mysql",
            "connector_id": "mysql-jdbc",
            "connection_type": "MYSQL",
            "config": {"host": "127.0.0.1", "port": 3306, "database": "sales", "password": "pw1"},
        },
    ).json()
    sink_conn = client.post(
        f"/api/v1/projects/{proj_prod_id}/connections",
        headers=maker_headers,
        json={
            "name": "agent-sink-doris",
            "connector_id": "doris",
            "connection_type": "DORIS",
            "config": {"host": "127.0.0.1", "port": 9030, "database": "dw", "password": "pw2"},
        },
    ).json()

    snap = client.post(
        f"/api/v1/projects/{proj_prod_id}/connections/{src_conn['id']}/metadata/probe",
        headers=maker_headers,
        json={
            "connector_version": "2.3.9",
            "override_objects": [
                {
                    "schema_name": "sales",
                    "object_name": "orders",
                    "object_type": "TABLE",
                    "primary_key": ["id"],
                    "columns": [
                        {"column_name": "id", "data_type": "BIGINT", "nullable": False},
                        {"column_name": "customer_email", "data_type": "VARCHAR(128)", "nullable": False},
                        {"column_name": "amount", "data_type": "DECIMAL(18,2)", "nullable": False},
                        {"column_name": "updated_at", "data_type": "DATETIME", "nullable": False},
                    ],
                }
            ],
        },
    ).json()

    # 2. 创建 LangGraph Agent Thread
    thread_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/agent/threads",
        headers=maker_headers,
        json={"title": "Orders Incremental Sync Thread"},
    )
    assert thread_resp.status_code == 201
    thread_id = thread_resp.json()["thread_id"]

    # 3. 发起 INCREMENTAL 需求但缺失 incremental_column -> 触发 CLARIFICATION_REQUIRED 中断
    invoke_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/agent/threads/{thread_id}/invoke",
        headers=maker_headers,
        json={
            "prompt": "增量同步 sales.orders 到 dw.orders",
            "source_connection_id": src_conn["id"],
            "sink_connection_id": sink_conn["id"],
            "metadata_snapshot_id": snap["snapshot_id"],
            "hints": {
                "source_table": "sales.orders",
                "sink_table": "dw.orders",
                "sync_mode": "INCREMENTAL",
                "write_mode": "SHADOW_SWAP",
                "primary_keys": ["id"],
            },
        },
    )
    assert invoke_resp.status_code == 200
    invoke_data = invoke_resp.json()
    assert invoke_data["status"] == "INTERRUPTED"
    assert invoke_data["pending_clarification"]["type"] == "CLARIFICATION_REQUIRED"
    assert "incremental_column" in invoke_data["pending_clarification"]["missing"]

    # 4. 跨租户（Org Beta）与跨用户（Auditor）企图恢复该线程 -> 均被 403 TENANT_ACCESS_DENIED 硬拦截
    cross_tenant_resp = client.post(
        f"/api/v1/projects/{proj_beta_id}/agent/threads/{thread_id}/resume",
        headers={
            "Authorization": f"Bearer {maker_tokens['access_token']}",
            "X-Organization-Id": org_beta_id,
        },
        json={"answers": {"incremental_column": "updated_at"}},
    )
    assert cross_tenant_resp.status_code == 403
    assert cross_tenant_resp.json()["error_code"] == ErrorCode.TENANT_ACCESS_DENIED.value

    cross_user_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/agent/threads/{thread_id}/resume",
        headers={
            "Authorization": f"Bearer {auditor_tokens['access_token']}",
            "X-Organization-Id": org_alpha_id,
        },
        json={"answers": {"incremental_column": "updated_at"}},
    )
    assert cross_user_resp.status_code == 403
    assert cross_user_resp.json()["error_code"] == ErrorCode.TENANT_ACCESS_DENIED.value

    # 5. 合法 Owner (Maker) 恢复线程并补齐 incremental_column -> 产出完整候选 EtlPlan/SQL/HOCON
    resume_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/agent/threads/{thread_id}/resume",
        headers=maker_headers,
        json={"answers": {"incremental_column": "updated_at"}},
    )
    assert resume_resp.status_code == 200, resume_resp.text
    resume_data = resume_resp.json()
    assert resume_data["status"] == "CANDIDATE_READY"
    candidate = resume_data["candidate"]
    assert candidate["gate"]["passed"] is True
    assert "${SECRET:SRC_PWD}" in candidate["hocon"]
    assert candidate["plan"]["source"]["objects"][0]["incremental_column"] == "updated_at"

    # 6. 创建 PipelineDefinition -> 调用 /drafts/validate 确定性门禁 -> 调用 /versions 冻结不可变版本与 Manifest
    pipe_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/pipelines",
        headers=maker_headers,
        json={
            "name": "orders-incremental-pipeline",
            "description": "Incremental sync from sales.orders to dw.orders",
            "sync_mode": "INCREMENTAL",
            "source_connection_id": src_conn["id"],
            "sink_connection_id": sink_conn["id"],
        },
    )
    assert pipe_resp.status_code == 201
    pipeline_id = pipe_resp.json()["id"]

    gate_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/pipelines/{pipeline_id}/drafts/validate",
        headers=maker_headers,
        json={"thread_id": thread_id, "candidate": candidate},
    )
    assert gate_resp.status_code == 200
    assert gate_resp.json()["passed"] is True

    freeze_resp = client.post(
        f"/api/v1/projects/{proj_prod_id}/pipelines/{pipeline_id}/versions",
        headers=maker_headers,
        json={"thread_id": thread_id},
    )
    assert freeze_resp.status_code == 201, freeze_resp.text
    frozen_ver = freeze_resp.json()
    version_id = frozen_ver["id"]
    assert frozen_ver["revision"] == 1
    assert frozen_ver["status"] == "FROZEN"
    assert frozen_ver["artifact_hash"].startswith("sha256:")

    manifest_resp = client.get(
        f"/api/v1/projects/{proj_prod_id}/pipeline-versions/{version_id}/manifest",
        headers=maker_headers,
    )
    assert manifest_resp.status_code == 200
    manifest_data = manifest_resp.json()
    assert manifest_data["artifact_hash"] == frozen_ver["artifact_hash"]
    assert '"schema_version":"1.0.0"' in manifest_data["canonical_json"]


def test_6_harness_security_flow_p1_dual_slots_sod_commit_evidence_and_rollback(
    client: TestClient,
    integration_db_factory: sessionmaker[Session],
    seeded_iam: dict[str, Any],
) -> None:
    """验收项 6：完整 Harness 安全闭环（P1 双槽分配 -> Maker 自批 403 -> 同人双槽 403 -> 双人审批通过 -> Operator Commit 原子消费 Capability + Outbox + Evidence -> 证据链验签 -> 受管回滚 Prepare -> Worker 幂等消费）。"""
    maker_tokens = _login_user(client, "maker@etl-agent.local")
    data_checker_tokens = _login_user(client, "data_checker@etl-agent.local")
    sec_checker_tokens = _login_user(client, "security_checker@etl-agent.local")
    operator_tokens = _login_user(client, "operator@etl-agent.local")

    org_id = str(seeded_iam["organizations"]["org-alpha"].id)
    proj_id = str(seeded_iam["projects"]["proj-orders-prod"].id)

    def _hdr(tokens: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
        base = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "X-Organization-Id": org_id,
        }
        if extra:
            base.update(extra)
        return base

    pipeline_id = str(uuid.uuid4())
    pipeline_version_id = str(uuid.uuid4())

    # 1. Maker 在 prod 项目发起 POST /preparations -> PDP 自动判定为 P1 并分配 DATA_CHECKER + SECURITY_CHECKER 双槽位
    prep_resp = client.post(
        f"/api/v1/projects/{proj_id}/preparations",
        headers=_hdr(maker_tokens),
        json={
            "preparation_type": "EXECUTION",
            "pipeline_id": pipeline_id,
            "pipeline_version_id": pipeline_version_id,
            "environment": "prod",
            "data_classification": "RESTRICTED_PII",
        },
    )
    assert prep_resp.status_code == 201, prep_resp.text
    prep_data = prep_resp.json()
    prep_id = prep_data["id"]
    assert prep_data["risk_level"] == "P1"
    assert prep_data["status"] == "PENDING_APPROVAL"
    assert set(prep_data["required_approval_slots"]) == {"DATA_CHECKER", "SECURITY_CHECKER"}

    # 2. Maker 企图自批自己发起的 Preparation -> 403 APPROVAL_SELF_APPROVE_FORBIDDEN
    self_approve_resp = client.post(
        f"/api/v1/projects/{proj_id}/preparations/{prep_id}/approvals",
        headers=_hdr(maker_tokens),
        json={"slot_role": "DATA_CHECKER", "decision": "APPROVE", "reason": "Self approve attempt"},
    )
    assert self_approve_resp.status_code == 403
    assert self_approve_resp.json()["error_code"] == ErrorCode.APPROVAL_SELF_APPROVE_FORBIDDEN.value

    # 3. Data Checker 审批槽位 1 (DATA_CHECKER) -> 201 Recorded
    slot1_resp = client.post(
        f"/api/v1/projects/{proj_id}/preparations/{prep_id}/approvals",
        headers=_hdr(data_checker_tokens),
        json={"slot_role": "DATA_CHECKER", "decision": "APPROVE", "reason": "Schema & DQ verified"},
    )
    assert slot1_resp.status_code == 201, slot1_resp.text
    assert slot1_resp.json()["preparation_status"] == "PENDING_APPROVAL"
    assert slot1_resp.json()["missing_slots"] == ["SECURITY_CHECKER"]

    # 4. 同一用户 (Data Checker) 企图继续审批槽位 2 (SECURITY_CHECKER) -> 403 APPROVAL_SLOT_CONFLICT
    conflict_resp = client.post(
        f"/api/v1/projects/{proj_id}/preparations/{prep_id}/approvals",
        headers=_hdr(data_checker_tokens),
        json={"slot_role": "SECURITY_CHECKER", "decision": "APPROVE", "reason": "Try second slot"},
    )
    assert conflict_resp.status_code == 403
    assert conflict_resp.json()["error_code"] == ErrorCode.APPROVAL_SLOT_CONFLICT.value

    # 5. Security Checker 审批槽位 2 (SECURITY_CHECKER) -> 双槽全部满足，Preparation 状态变为 APPROVED
    slot2_resp = client.post(
        f"/api/v1/projects/{proj_id}/preparations/{prep_id}/approvals",
        headers=_hdr(sec_checker_tokens),
        json={"slot_role": "SECURITY_CHECKER", "decision": "APPROVE", "reason": "PII masking & budget verified"},
    )
    assert slot2_resp.status_code == 201, slot2_resp.text
    assert slot2_resp.json()["preparation_status"] == "APPROVED"
    assert slot2_resp.json()["missing_slots"] == []

    # 6. Operator 携带 Idempotency-Key 执行 POST /commit -> 202 Accepted
    idem_key = f"idem-commit-{uuid.uuid4().hex[:16]}"
    commit_resp = client.post(
        f"/api/v1/projects/{proj_id}/preparations/{prep_id}/commit",
        headers=_hdr(operator_tokens, {"Idempotency-Key": idem_key}),
        json={"comment": "Approved production batch release"},
    )
    assert commit_resp.status_code == 202, commit_resp.text
    commit_data = commit_resp.json()
    execution_id = commit_data["execution_id"]
    command_id = commit_data["command_id"]
    capability_jti = commit_data["capability_jti"]
    outbox_event_id = commit_data["outbox_event_id"]
    assert commit_data["status"] == "QUEUED"

    # 验证数据库中已原子持久化 CapabilityIssuance、CapabilityConsumption、ExecutionCommand、OutboxEvent、OutboxDelivery 与 EvidenceEvent
    session = integration_db_factory()
    try:
        jti_uuid = uuid.UUID(capability_jti)
        cmd_uuid = uuid.UUID(command_id)
        outbox_uuid = uuid.UUID(outbox_event_id)

        assert session.execute(
            select(CapabilityIssuance).where(CapabilityIssuance.jti == jti_uuid)
        ).scalar_one_or_none() is not None
        assert session.execute(
            select(CapabilityConsumption).where(CapabilityConsumption.jti == jti_uuid)
        ).scalar_one_or_none() is not None
        cmd_row = session.execute(
            select(ExecutionCommand).where(ExecutionCommand.command_id == cmd_uuid)
        ).scalar_one()
        assert cmd_row.status == "PENDING"
        outbox_row = session.get(OutboxEvent, outbox_uuid)
        assert outbox_row is not None
        deliv_row = session.execute(
            select(OutboxDelivery).where(OutboxDelivery.outbox_event_id == outbox_uuid)
        ).scalar_one()
        assert deliv_row.status == "PENDING"
        ev_rows = (
            session.execute(
                select(EvidenceEvent).where(
                    EvidenceEvent.organization_id == uuid.UUID(org_id),
                    EvidenceEvent.project_id == uuid.UUID(proj_id),
                )
            )
            .scalars()
            .all()
        )
        assert len(ev_rows) >= 3

        # 验证 Celery Worker 任务真实消费该 Outbox 事件，且二次重投返回 IDEMPOTENT_NOOP
        outbox_payload = dict(outbox_row.payload_ref_json)
        worker_res_1 = execute_batch_command(outbox_payload, session=session)
        session.commit()
        assert worker_res_1["status"] == "SUBMITTED"
        assert worker_res_1["external_job_id"].startswith("zeta-")

        worker_res_2 = execute_batch_command(outbox_payload, session=session)
        assert worker_res_2["status"] == "IDEMPOTENT_NOOP"
        assert worker_res_2["side_effects_executed"] is False
    finally:
        session.close()

    # 7. Evidence Ledger POST /evidence/verify -> chain_valid: true, signatures_valid: true
    verify_ev_resp = client.post(
        f"/api/v1/projects/{proj_id}/evidence/verify",
        headers=_hdr(operator_tokens),
    )
    assert verify_ev_resp.status_code == 200, verify_ev_resp.text
    verify_ev_data = verify_ev_resp.json()
    assert verify_ev_data["chain_valid"] is True
    assert verify_ev_data["signatures_valid"] is True
    assert verify_ev_data["verified_events"] >= 3
    assert verify_ev_data["checkpoints_verified"] >= 1

    # 8. Rollback POST /rollbacks/prepare -> 强制经过 Harness Preparation（零直接 SQL 执行）
    rb_prep_resp = client.post(
        f"/api/v1/projects/{proj_id}/rollbacks/prepare",
        headers=_hdr(maker_tokens),
        json={
            "rollback_type": "SHADOW_CLEANUP",
            "target_execution_id": execution_id,
            "shadow_table_name": "dw.orders__shadow_test01",
        },
    )
    assert rb_prep_resp.status_code == 201, rb_prep_resp.text
    rb_prep_data = rb_prep_resp.json()
    assert rb_prep_data["preparation_type"] == "ROLLBACK"
    assert rb_prep_data["rollback_type"] == "SHADOW_CLEANUP"
    assert rb_prep_data["status"] == "PENDING_APPROVAL"
    assert set(rb_prep_data["required_approval_slots"]) == {"DATA_CHECKER", "SECURITY_CHECKER"}


def test_7_benchmark_dataset_generation_and_p0_p1_independent_hard_gate(
    client: TestClient,
    seeded_iam: dict[str, Any],
) -> None:
    """验收项 7：POST /benchmarks/datasets 生成确定性数据集 -> POST /benchmarks/runs 运行 L0/L1 套件并强制执行 P0/P1 独立硬门槛。"""
    admin_tokens = _login_user(client, "admin@etl-agent.local")
    org_id = str(seeded_iam["organizations"]["org-alpha"].id)
    proj_id = str(seeded_iam["projects"]["proj-orders-prod"].id)
    headers = {
        "Authorization": f"Bearer {admin_tokens['access_token']}",
        "X-Organization-Id": org_id,
    }

    # 1. 生成固定种子 (seed=42) 的 10K 确定性基准数据集
    ds_resp = client.post(
        f"/api/v1/projects/{proj_id}/benchmarks/datasets",
        headers=headers,
        json={
            "name": "logs-anomaly-10k-seed42",
            "scenario": "logs_anomaly",
            "row_tier": "10K",
            "seed": 42,
            "error_rate": 0.25,
            "formats": ["csv", "jsonl"],
        },
    )
    assert ds_resp.status_code == 201, ds_resp.text
    ds_data = ds_resp.json()
    dataset_id = ds_data["id"]
    assert ds_data["total_rows"] == 10_000
    assert ds_data["actual_error_rows"] == 2_500
    assert ds_data["manifest_sha256"].startswith("sha256:")

    # 2. 触发真实 L0 + L1 基准评测运行 -> 全部 P0/P1 拦截率为 100%，overall_passed 为 True
    run_ok_resp = client.post(
        f"/api/v1/projects/{proj_id}/benchmarks/runs",
        headers=headers,
        json={"dataset_id": dataset_id, "layer": "L0", "is_mock_llm": True},
    )
    assert run_ok_resp.status_code == 202, run_ok_resp.text
    run_ok = run_ok_resp.json()
    assert run_ok["p0_passed"] is True
    assert run_ok["p1_passed"] is True
    assert run_ok["overall_passed"] is True
    assert run_ok["l0_cases_executed"] >= 7
    assert run_ok["l1_cases_executed"] >= 6

    # 3. 验证 P0/P1 独立一票否决硬门槛：即使数据生成准确率 100%，只要 P1 未通过，overall_passed 必须为 False
    run_fail_resp = client.post(
        f"/api/v1/projects/{proj_id}/benchmarks/runs",
        headers=headers,
        json={
            "dataset_id": dataset_id,
            "layer": "L0",
            "is_mock_llm": True,
            "p0_passed": True,
            "p1_passed": False,
        },
    )
    assert run_fail_resp.status_code == 202
    run_fail = run_fail_resp.json()
    assert run_fail["p0_passed"] is True
    assert run_fail["p1_passed"] is False
    assert run_fail["overall_passed"] is False
    assert len(run_fail["gate_failure_reasons"]) >= 1

    # 4. 验证 Celery 任务注册表完整性
    registered_tasks = set(celery_app.tasks.keys())
    expected_tasks = {
        "etl_agent.data_plane.execute_batch_command",
        "etl_agent.data_plane.execute_cdc_command",
        "etl_agent.data_plane.cancel_execution_command",
        "etl_agent.data_plane.publish_swap_command",
        "etl_agent.data_plane.rollback_shadow_cleanup",
        "etl_agent.data_plane.rollback_swap_restore",
        "etl_agent.data_plane.rollback_cdc_recover",
        "etl_agent.data_plane.reconcile_unknown_run",
        "etl_agent.outbox.dispatch_pending_events",
        "etl_agent.scheduling.evaluate_due_schedules",
    }
    assert expected_tasks <= registered_tasks
    assert callable(dispatch_pending_events)
    assert callable(evaluate_due_schedules)
    assert callable(reconcile_unknown_run)
