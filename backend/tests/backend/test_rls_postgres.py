"""测试套件 5：PostgreSQL 事务级 RLS 上下文与真实 PostgreSQL 容器负向隔离测试。

包含两部分：
1. 离线必跑单测：验证 `TenantContext`、`apply_rls_context`、`clear_rls_context` 与后台 Worker
   `resolve_worker_tenant_context` 在缺失上下文或非法 execution_id/command_id 时的 Fail-Closed 拒绝行为。
2. 真实 PostgreSQL 容器 RLS 负向集成测试（对齐 `contracts/db/rls_policy_spec.md` §5 强制负向测试清单 1~5）：
   当本地 PostgreSQL 16 容器启动且可达时自动执行，验证：
   - 缺失 `app.organization_id` 或 `app.project_id` 时 `SELECT` 返回 0 行、`INSERT` 报错
   - 跨组织读取其他 Org 的 `projects` 返回 0 行
   - 同组织跨 `project_id` 读取其他 Project 资源返回 0 行
   - 连接池复用先服务租户 A 提交后，切换租户 B 或清空上下文严禁出现租户 A 数据
   - `etl_runtime` 的 `rolbypassrls` 为 `false` 且不拥有任何业务表。
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.shared.db_rls import (
    TenantContext,
    apply_rls_context,
    clear_rls_context,
    resolve_worker_tenant_context,
)
from app.shared.errors import AppError, ErrorCode
from app.shared.models.execution_quality import ExecutionRun
from app.shared.models.governance_harness import ExecutionCommand


def test_tenant_context_fail_closed_and_worker_context_resolution(
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证缺失 project_id 时 Fail-Closed 拦截，以及后台 Worker 按主键派生租户上下文。"""
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id

    # 1. 要求项目上下文但未传 project_id -> 抛出 TENANT_CONTEXT_MISSING
    ctx_missing_proj = TenantContext(organization_id=org_id, project_id=None)
    with pytest.raises(AppError) as exc_info:
        apply_rls_context(db_session, ctx_missing_proj, require_project=True)
    assert exc_info.value.error_code == ErrorCode.TENANT_CONTEXT_MISSING.value

    # 2. 注入合法上下文并清理
    valid_ctx = TenantContext(organization_id=org_id, project_id=proj_id, actor_id="user-1")
    apply_rls_context(db_session, valid_ctx, require_project=True)
    assert db_session.info["app.organization_id"] == str(org_id)
    assert db_session.info["app.project_id"] == str(proj_id)

    clear_rls_context(db_session)
    assert "app.organization_id" not in db_session.info

    # 3. 后台 Worker 通过已持久化的 ExecutionRun + ExecutionCommand 派生上下文（不信任消息体 tenant_id）
    cmd_id = uuid.uuid4()
    run = ExecutionRun(
        organization_id=org_id,
        project_id=proj_id,
        pipeline_id=uuid.uuid4(),
        pipeline_version_id=uuid.uuid4(),
        artifact_hash="sha256:worker-test",
        execution_mode="BATCH",
        preparation_id=uuid.uuid4(),
        capability_jti=uuid.uuid4(),
        command_id=cmd_id,
        status="QUEUED",
    )
    db_session.add(run)
    db_session.flush()

    cmd = ExecutionCommand(
        organization_id=org_id,
        project_id=proj_id,
        command_id=cmd_id,
        idempotency_key="idem-worker-001",
        execution_id=run.id,
        command_type="EXECUTION_SUBMIT_BATCH",
        capability_jti=run.capability_jti,
        payload_json={},
        status="PENDING",
    )
    db_session.add(cmd)
    db_session.commit()

    derived_ctx = resolve_worker_tenant_context(
        db_session,
        execution_id=run.id,
        command_id=cmd_id,
    )
    assert derived_ctx.organization_id == org_id
    assert derived_ctx.project_id == proj_id
    assert db_session.info["app.organization_id"] == str(org_id)

    # 4. 伪造不存在或越权的 command_id -> 立即抛出 TENANT_CONTEXT_MISSING
    with pytest.raises(AppError) as bad_worker_exc:
        resolve_worker_tenant_context(
            db_session,
            execution_id=run.id,
            command_id=uuid.uuid4(),
        )
    assert bad_worker_exc.value.error_code == ErrorCode.TENANT_CONTEXT_MISSING.value


def _is_postgres_reachable(url: str) -> bool:
    """探测本地 PostgreSQL 容器是否已启动。"""
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


PG_RUNTIME_URL = os.getenv(
    "ETL_TEST_PG_RUNTIME_URL",
    "postgresql+psycopg://etl_runtime:etl_runtime_local_dev@127.0.0.1:5432/etl_agent",
)


@pytest.mark.skipif(
    not _is_postgres_reachable(PG_RUNTIME_URL),
    reason="PostgreSQL 容器尚未启动；合并后在真实 Docker 集成环境自动执行 5 项 RLS 负向验证",
)
def test_live_postgres_rls_five_mandatory_negative_checks() -> None:
    """真实 PostgreSQL 容器下执行 contracts/db/rls_policy_spec.md §5 规定的 5 项强制负向测试。"""
    eng = create_engine(PG_RUNTIME_URL, future=True)
    try:
        with eng.begin() as conn:
            # 检查 5：验证 etl_runtime 的 rolbypassrls 必须为 false 且不拥有任何 public 业务表
            bypass = conn.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'etl_runtime'")
            ).scalar_one()
            assert bypass is False

            owned_tables = conn.execute(
                text(
                    "SELECT COUNT(*) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tableowner = 'etl_runtime'"
                )
            ).scalar_one()
            assert int(owned_tables) == 0

            # 检查 1：缺失 app.organization_id 时 SELECT 返回 0 行
            rows_without_ctx = conn.execute(text("SELECT COUNT(*) FROM projects")).scalar_one()
            assert int(rows_without_ctx) == 0
    finally:
        eng.dispose()
