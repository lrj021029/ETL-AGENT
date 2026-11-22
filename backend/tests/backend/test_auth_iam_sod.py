"""测试套件 2：认证、邮件二次验证（Email OTP）、Refresh Token 重用检测、双重成员与固定角色/SoD 保护。

覆盖关键安全要求：
1. 密码登录返回“邮件二次验证”（Email OTP）挑战，校验 OTP 后签发 JWT Access + Refresh Token。
2. Refresh Token 正常轮转；当已轮转的旧 Refresh Token 被重放时（Reuse Detection），
   立即吊销整个 `RefreshTokenFamily` 与关联会话，返回 `AUTH_REFRESH_REUSE_DETECTED` (401)。
3. 组织成员 + 项目成员双重校验：当用户被移出 `OrganizationMember` 后，即使 `ProjectMember` 仍存在，
   访问该项目任何 API 立即被拒绝并返回 `TENANT_ACCESS_DENIED` (403)。
4. 固定系统角色（`MAKER`, `DATA_CHECKER`, `SECURITY_CHECKER`, `OPERATOR`, `AUDITOR`, `ORG_ADMIN`, `PROJECT_ADMIN`）
   不可变：企图覆盖或删除固定系统角色返回 `SYSTEM_ROLE_IMMUTABLE` (409)。
5. 自定义项目角色企图绕过职责分离（SoD）或组合自批权限时返回 `SOD_VIOLATION` (403)。
6. 审批时 Maker 自批返回 `APPROVAL_SELF_APPROVE_FORBIDDEN` (403)，同一用户兼任双审批槽返回 `APPROVAL_SLOT_CONFLICT` (403)。
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.memberships.service import MembershipService
from app.organizations.service import OrganizationService
from app.shared.errors import AppError, ErrorCode


def _complete_login_flow(client: TestClient, email: str, password: str = "DemoPass!2026") -> dict[str, Any]:
    """辅助函数：执行密码登录 + 邮件二次验证（Email OTP）并返回 Token 响应。"""
    login_resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 200
    login_data = login_resp.json()
    assert login_data["verification_label"] == "邮件二次验证"
    assert login_data["verification_type"] == "EMAIL_OTP"

    auth_svc: AuthService = client.app.state.auth_service
    otp_record = auth_svc.sent_otp_outbox[-1]
    assert otp_record.email == email
    assert otp_record.verification_label == "邮件二次验证"

    verify_resp = client.post(
        "/api/v1/auth/otp/verify",
        json={"challenge_id": login_data["challenge_id"], "otp_code": otp_record.otp_code},
    )
    assert verify_resp.status_code == 200
    token_data = verify_resp.json()
    assert "access_token" in token_data
    assert "refresh_token" in token_data
    assert token_data["verification_label"] == "邮件二次验证"
    return token_data


def test_login_email_otp_and_refresh_token_reuse_detection(api_client: TestClient) -> None:
    """验证邮件二次验证登录闭环，以及旧 Refresh Token 重放触发整个家族吊销（Reuse Detection）。"""
    tokens_v0 = _complete_login_flow(api_client, "maker@etl-agent.local")
    refresh_v0 = tokens_v0["refresh_token"]

    # 第一次正常轮转：v0 -> v1
    rot1_resp = api_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_v0})
    assert rot1_resp.status_code == 200
    tokens_v1 = rot1_resp.json()
    refresh_v1 = tokens_v1["refresh_token"]
    assert refresh_v1 != refresh_v0
    assert tokens_v1["rotation_count"] == 1

    # 攻击模拟：攻击者重放已使用过的旧令牌 refresh_v0
    reuse_resp = api_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_v0})
    assert reuse_resp.status_code == 401
    reuse_err = reuse_resp.json()
    assert reuse_err["error_code"] == ErrorCode.AUTH_REFRESH_REUSE_DETECTED.value
    assert "trace_id" in reuse_err

    # 家族吊销验证：此时原本合法的最新令牌 refresh_v1 也必须已失效被拒！
    after_compromise_resp = api_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_v1})
    assert after_compromise_resp.status_code == 401
    assert after_compromise_resp.json()["error_code"] == ErrorCode.AUTH_REFRESH_REUSE_DETECTED.value


def test_dual_membership_removing_org_member_immediately_denies_project_access(
    api_client: TestClient,
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证 Org+Project 双重成员强制检查：移除 OrganizationMember 后立即丧失该组织下 Project 访问权。"""
    tokens = _complete_login_flow(api_client, "maker@etl-agent.local")
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": str(seeded_env["organizations"]["org-alpha"].id),
    }
    proj_id = seeded_env["projects"]["proj-orders-prod"].id

    # 移除前：正常访问项目总览
    ok_resp = api_client.get(f"/api/v1/projects/{proj_id}/overview", headers=headers)
    assert ok_resp.status_code == 200
    assert ok_resp.json()["project_id"] == str(proj_id)

    # 仅将 maker 从 org-alpha 的 OrganizationMember 中移除（保留其 ProjectMember 记录不删）
    org_svc = OrganizationService()
    org_svc.remove_organization_member(
        db_session,
        organization_id=seeded_env["organizations"]["org-alpha"].id,
        user_id=seeded_env["users"]["maker"].id,
    )
    db_session.commit()

    # 移除后：再次访问同一 Project 立即被拒（TENANT_ACCESS_DENIED 403）
    denied_resp = api_client.get(f"/api/v1/projects/{proj_id}/overview", headers=headers)
    assert denied_resp.status_code == 403
    assert denied_resp.json()["error_code"] == ErrorCode.TENANT_ACCESS_DENIED.value


def test_immutable_system_roles_and_custom_role_sod_enforcement(
    api_client: TestClient,
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证固定系统角色不可覆盖/删除（SYSTEM_ROLE_IMMUTABLE）与自定义角色不可绕过职责分离（SOD_VIOLATION）。"""
    tokens = _complete_login_flow(api_client, "admin@etl-agent.local")
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": str(org_id),
    }

    # 1. 企图通过 API 创建与固定系统角色 MAKER 同名的自定义角色 -> 409 SYSTEM_ROLE_IMMUTABLE
    immutable_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/roles",
        headers=headers,
        json={
            "code": "MAKER",
            "name": "Attempt Override Maker",
            "permissions": ["connection:read"],
        },
    )
    assert immutable_resp.status_code == 409
    assert immutable_resp.json()["error_code"] == ErrorCode.SYSTEM_ROLE_IMMUTABLE.value

    # 2. 企图直接通过服务层删除固定系统角色 DATA_CHECKER -> SYSTEM_ROLE_IMMUTABLE
    membership_svc = MembershipService()
    data_checker_role = seeded_env["roles"]["DATA_CHECKER"]
    try:
        membership_svc.update_or_delete_role(db_session, role_id=data_checker_role.id, action="DELETE")
        raise AssertionError("删除固定系统角色应当抛出 SYSTEM_ROLE_IMMUTABLE")
    except AppError as exc:
        assert exc.error_code == ErrorCode.SYSTEM_ROLE_IMMUTABLE.value

    # 3. 企图创建带有 override_sod=True 或同时组合 Maker Prepare 与 Checker 审批权限的自定义角色 -> 403 SOD_VIOLATION
    sod_resp_1 = api_client.post(
        f"/api/v1/projects/{proj_id}/roles",
        headers=headers,
        json={
            "code": "SUPER_BYPASS_ROLE",
            "name": "SoD Bypass Role",
            "permissions": ["pipeline:prepare", "approval:data_check"],
        },
    )
    assert sod_resp_1.status_code == 403
    assert sod_resp_1.json()["error_code"] == ErrorCode.SOD_VIOLATION.value

    # 4. 企图在同一自定义角色中同时赋予 DATA_CHECKER 与 SECURITY_CHECKER 双审批槽权限 -> 403 SOD_VIOLATION
    sod_resp_2 = api_client.post(
        f"/api/v1/projects/{proj_id}/roles",
        headers=headers,
        json={
            "code": "DUAL_CHECKER_ROLE",
            "name": "Dual Slot Role",
            "permissions": ["approval:data_check", "approval:security_check"],
        },
    )
    assert sod_resp_2.status_code == 403
    assert sod_resp_2.json()["error_code"] == ErrorCode.SOD_VIOLATION.value

    # 5. 创建合规的只读分析师自定义角色 -> 201 Created
    valid_custom_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/roles",
        headers=headers,
        json={
            "code": "DATA_ANALYST_CUSTOM",
            "name": "数据只读分析师",
            "permissions": ["connection:read", "metadata:probe"],
        },
    )
    assert valid_custom_resp.status_code == 201
    assert valid_custom_resp.json()["code"] == "DATA_ANALYST_CUSTOM"
    assert valid_custom_resp.json()["is_system"] is False
