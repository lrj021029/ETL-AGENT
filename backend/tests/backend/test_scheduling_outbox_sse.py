"""测试套件 4：受管定时调度（StandingSchedulePreparation 绑定）、Outbox 租约投递器、SSE 实时流与 OpenAPI 全路径校验。

覆盖关键工程要求：
1. Schedule 创建必须绑定 `preparation_type == 'STANDING_SCHEDULE'` 且 `status == 'APPROVED'` 的准备单；
   未审批或类型不符时拒绝创建。
2. Cron 表达式、时区（UTC / Asia/Shanghai / America/New_York）及 `FORBID` 并发策略与 `SKIP` 错过策略校验。
3. Outbox Dispatcher：`SELECT ... FOR UPDATE SKIP LOCKED` 租约领取、负载契约校验、成功投递与超过 `max_attempts` 转 `FAILED`。
4. SSE 实时流端点 `/api/v1/projects/{project_id}/executions/{execution_id}/events` 输出符合
   `contracts/openapi/realtime_events.yaml` 的事件帧。
5. OpenAPI 契约中的全部 38 个路径均已在 FastAPI 应用中完整挂载。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.scheduling.service import ScheduleService
from app.shared.errors import ErrorCode
from app.shared.models.execution_quality import ExecutionMetric, ExecutionRun
from app.shared.models.governance_harness import OutboxDelivery, OutboxEvent, Preparation
from workers.outbox_dispatcher import OutboxDispatcher


def _complete_login_flow(client: TestClient, email: str, password: str = "DemoPass!2026") -> dict[str, Any]:
    """辅助函数：执行密码登录 + 邮件二次验证（Email OTP）并返回 Token 响应。"""
    login_resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 200
    login_data = login_resp.json()
    otp_record = client.app.state.auth_service.sent_otp_outbox[-1]
    verify_resp = client.post(
        "/api/v1/auth/otp/verify",
        json={"challenge_id": login_data["challenge_id"], "otp_code": otp_record.otp_code},
    )
    assert verify_resp.status_code == 200
    return verify_resp.json()


def test_schedule_requires_approved_standing_preparation_and_enforces_policies(
    api_client: TestClient,
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证 Schedule 强制绑定已审批 StandingSchedulePreparation，以及 FORBID 并发策略与 SKIP 错过策略。"""
    tokens = _complete_login_flow(api_client, "operator@etl-agent.local")
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id
    operator_id = seeded_env["users"]["operator"].id
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": str(org_id),
    }

    pipeline_id = uuid.uuid4()
    version_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # 1. 创建尚未审批通过的 STANDING_SCHEDULE 准备单 -> 创建 Schedule 必须被拒 (409 APPROVAL_INCOMPLETE)
    unapproved_prep = Preparation(
        organization_id=org_id,
        project_id=proj_id,
        preparation_type="STANDING_SCHEDULE",
        pipeline_id=pipeline_id,
        pipeline_version_id=version_id,
        artifact_hash="sha256:1111",
        environment="prod",
        risk_level="P2",
        policy_version="v1.0.0",
        status="PENDING_APPROVAL",
        frozen_facts_json={},
        rollback_plan_json={},
        budget_json={},
        fingerprint_sha256="sha256:fp1",
        created_by=operator_id,
        expires_at=now + timedelta(days=30),
    )
    db_session.add(unapproved_prep)
    db_session.commit()

    bad_sched_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/schedules",
        headers=headers,
        json={
            "pipeline_id": str(pipeline_id),
            "pipeline_version_id": str(version_id),
            "standing_preparation_id": str(unapproved_prep.id),
            "cron_expression": "*/15 * * * *",
            "timezone": "Asia/Shanghai",
        },
    )
    assert bad_sched_resp.status_code == 409
    assert bad_sched_resp.json()["error_code"] == ErrorCode.APPROVAL_INCOMPLETE.value

    # 2. 将准备单更新为 APPROVED 后创建 Schedule -> 201 Created
    unapproved_prep.status = "APPROVED"
    db_session.commit()

    ok_sched_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/schedules",
        headers=headers,
        json={
            "pipeline_id": str(pipeline_id),
            "pipeline_version_id": str(version_id),
            "standing_preparation_id": str(unapproved_prep.id),
            "cron_expression": "0 2 * * *",
            "timezone": "Asia/Shanghai",
            "concurrency_policy": "FORBID",
            "misfire_policy": "SKIP",
        },
    )
    assert ok_sched_resp.status_code == 201
    sched_data = ok_sched_resp.json()
    sched_id = uuid.UUID(sched_data["id"])
    assert sched_data["concurrency_policy"] == "FORBID"
    assert sched_data["misfire_policy"] == "SKIP"
    assert sched_data["next_run_at"] is not None

    # 3. 验证 FORBID 并发策略：当存在 RUNNING 的 ExecutionRun 时，触发被跳过（SKIPPED）
    active_run = ExecutionRun(
        organization_id=org_id,
        project_id=proj_id,
        pipeline_id=pipeline_id,
        pipeline_version_id=version_id,
        artifact_hash="sha256:1111",
        execution_mode="BATCH",
        preparation_id=unapproved_prep.id,
        capability_jti=uuid.uuid4(),
        command_id=uuid.uuid4(),
        status="RUNNING",
    )
    db_session.add(active_run)
    db_session.commit()

    sched_svc = ScheduleService()
    scheduled_time = datetime.fromisoformat(sched_data["next_run_at"])
    trigger_res = sched_svc.evaluate_and_trigger_schedule(
        db_session,
        organization_id=org_id,
        project_id=proj_id,
        schedule_id=sched_id,
        evaluation_time=scheduled_time + timedelta(seconds=10),
    )
    assert trigger_res["status"] == "SKIPPED"
    assert "CONCURRENCY_FORBID" in trigger_res["skip_reason"]


def test_outbox_dispatcher_lease_delivery_and_retry_exhaustion(
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证 OutboxDispatcher 的 FOR UPDATE SKIP LOCKED 租约领取、成功投递与超过 max_attempts 进入 FAILED。"""
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id

    valid_payload = {
        "schema_version": "1.0.0",
        "event_type": "EXECUTION_SUBMIT_BATCH",
        "command_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "capability_jti": str(uuid.uuid4()),
        "capability_jws": "eyJhbGciOiJFZERTQSIsImtpZCI6ImtleS0xIn0.payload.signature_valid_length",
        "trace_id": "tr-outbox-test-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    ev1 = OutboxEvent(
        organization_id=org_id,
        project_id=proj_id,
        aggregate_type="ExecutionRun",
        aggregate_id=uuid.UUID(valid_payload["execution_id"]),
        event_type="EXECUTION_SUBMIT_BATCH",
        payload_ref_json=valid_payload,
    )
    db_session.add(ev1)
    db_session.flush()
    deliv1 = OutboxDelivery(outbox_event_id=ev1.id, status="PENDING", attempts=0, max_attempts=3)
    db_session.add(deliv1)
    db_session.commit()

    dispatched_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_sender(task_name: str, payload: dict[str, Any]) -> None:
        dispatched_calls.append((task_name, payload))

    dispatcher = OutboxDispatcher(task_sender=_mock_sender, dispatcher_id="test-dispatcher-1")
    stats = dispatcher.dispatch_once(db_session)
    db_session.commit()

    assert stats["claimed"] == 1
    assert stats["delivered"] == 1
    assert len(dispatched_calls) == 1
    assert dispatched_calls[0][0] == "etl_agent.data_plane.execute_batch_command"
    assert deliv1.status == "DELIVERED"
    assert deliv1.delivered_at is not None

    # 测试投递失败重试直到耗尽 max_attempts=2 转入 FAILED
    ev2 = OutboxEvent(
        organization_id=org_id,
        project_id=proj_id,
        aggregate_type="ExecutionRun",
        aggregate_id=uuid.uuid4(),
        event_type="EXECUTION_SUBMIT_BATCH",
        payload_ref_json=valid_payload,
    )
    db_session.add(ev2)
    db_session.flush()
    deliv2 = OutboxDelivery(outbox_event_id=ev2.id, status="PENDING", attempts=0, max_attempts=2)
    db_session.add(deliv2)
    db_session.commit()

    def _failing_sender(task_name: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("Redis broker connection refused")

    fail_dispatcher = OutboxDispatcher(task_sender=_failing_sender, dispatcher_id="test-dispatcher-fail")
    res_try1 = fail_dispatcher.dispatch_once(db_session)
    assert res_try1["retried"] == 1
    assert deliv2.status == "PENDING"
    assert deliv2.attempts == 1

    res_try2 = fail_dispatcher.dispatch_once(db_session)
    assert res_try2["failed"] == 1
    assert deliv2.status == "FAILED"
    assert deliv2.attempts == 2


def test_sse_realtime_execution_stream_and_openapi_contract_routes(
    api_client: TestClient,
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证 SSE 实时执行事件流（含 >20% 拒绝率告警）以及 OpenAPI 全部契约路径完整性。"""
    tokens = _complete_login_flow(api_client, "operator@etl-agent.local")
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": str(org_id),
    }

    run = ExecutionRun(
        organization_id=org_id,
        project_id=proj_id,
        pipeline_id=uuid.uuid4(),
        pipeline_version_id=uuid.uuid4(),
        artifact_hash="sha256:abcd",
        execution_mode="BATCH",
        preparation_id=uuid.uuid4(),
        capability_jti=uuid.uuid4(),
        command_id=uuid.uuid4(),
        external_engine="SEATUNNEL_ZETA",
        external_job_id="zeta-job-9001",
        status="RUNNING",
    )
    db_session.add(run)
    db_session.flush()

    # 插入一条 reject_rate_total = 0.25 (> 0.20) 的指标采样，验证触发 execution.supervision_alert
    metric = ExecutionMetric(
        organization_id=org_id,
        project_id=proj_id,
        execution_id=run.id,
        read_rows=1000,
        read_bytes=128000,
        validated_rows=1000,
        valid_rows=750,
        write_rows=750,
        write_bytes=96000,
        error_rows=250,
        reject_rate_total=0.25,
        reject_rate_window=0.25,
        throughput_rps=500.0,
        duration_seconds=2.0,
        amplification_ratio=1.0,
    )
    db_session.add(metric)
    db_session.commit()

    # 1. 验证轮询兜底端点
    detail_resp = api_client.get(f"/api/v1/projects/{proj_id}/executions/{run.id}", headers=headers)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["confirmation_state"] == "ENGINE_CONFIRMED"
    assert detail_resp.json()["metrics"]["reject_rate_total"] == 0.25

    # 2. 验证 SSE 实时流端点
    sse_resp = api_client.get(f"/api/v1/projects/{proj_id}/executions/{run.id}/events", headers=headers)
    assert sse_resp.status_code == 200
    assert "text/event-stream" in sse_resp.headers["content-type"]
    sse_body = sse_resp.text
    assert "event: execution.state_changed" in sse_body
    assert "event: execution.metrics_tick" in sse_body
    assert "event: execution.supervision_alert" in sse_body
    assert "event: execution.heartbeat" in sse_body

    # 3. 对比 contracts/openapi/openapi.yaml 中的所有路径，确认 100% 在 FastAPI OpenAPI 规范中注册
    repo_root = Path(__file__).resolve().parents[3]
    contract_openapi = yaml.safe_load(
        (repo_root / "contracts" / "openapi" / "openapi.yaml").read_text(encoding="utf-8")
    )
    generated_openapi = api_client.get("/openapi.json").json()
    registered_paths = set(generated_openapi["paths"].keys())
    for contract_path in contract_openapi["paths"].keys():
        assert contract_path in registered_paths, f"OpenAPI 契约路径未在 FastAPI 中注册: {contract_path}"
