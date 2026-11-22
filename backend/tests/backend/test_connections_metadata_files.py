"""测试套件 3：数据源连接凭据零明文保护、文件路径逃逸守卫与不可变 MetadataSnapshot + SchemaDrift 对比引擎。

覆盖关键工程要求：
1. 连接创建与查询时，密码、连接 URI 内嵌密码、API Key 绝不以明文存入 `data_connections.config_non_secret_json`，
   也绝不在 API 响应中回显明文。
2. 文件上传路径穿越守卫（Path-Traversal Guard）：
   - 拦截 `../`、`..\\`、URL 编码 `%2e%2e%2f`
   - 拦截 POSIX 绝对路径 `/etc/passwd`、Windows 绝对路径 `C:\\Windows\\System32\\cmd.exe`、UNC 路径
   - 拦截指向受控根目录之外的符号链接（Symlink Escape）
   - 全部返回 `FILE_PATH_TRAVERSAL_BLOCKED` (403)。
3. 元数据探查与刷新：
   - 每次刷新始终插入全新的不可变 `MetadataSnapshot`（新 `snapshot_id`，旧快照保持原样）。
   - 自动识别敏感列（如 `email`, `phone`, `password_hash`, `id_card`）并对样本行脱敏存储。
   - 支持人工标记敏感分类（`MANUAL_MARK`）。
   - 自动计算两版快照之间的 `SchemaDriftEvent`（检测新增列、删除列、类型变更并判定严重度）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connections.service import validate_safe_file_path
from app.shared.errors import AppError, ErrorCode
from app.shared.models.connections_metadata import DataConnection


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


def test_connection_zero_plaintext_secrets_in_db_and_api(
    api_client: TestClient,
    db_session: Session,
    seeded_env: dict[str, Any],
) -> None:
    """验证创建和查询连接时，数据库业务表与 API 响应中均不包含任何明文密码。"""
    tokens = _complete_login_flow(api_client, "maker@etl-agent.local")
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": str(org_id),
    }

    raw_password = "SuperSecretMySQLPassword!9988"
    create_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/connections",
        headers=headers,
        json={
            "name": "prod-orders-mysql",
            "connector_id": "mysql-jdbc",
            "connection_type": "MYSQL",
            "config": {
                "host": "demo-mysql",
                "port": 3306,
                "database": "orders_db",
                "username": "etl_reader",
                "password": raw_password,
                "jdbc_uri": f"mysql://etl_reader:{raw_password}@demo-mysql:3306/orders_db",
            },
        },
    )
    assert create_resp.status_code == 201
    body_text = create_resp.text
    assert raw_password not in body_text

    conn_data = create_resp.json()
    assert conn_data["secret_ref_id"] is not None
    assert conn_data["secret_masked_preview"] == "******"

    # 直接检查数据库底层 data_connections 表，确认 config_non_secret_json 中不含明文密码
    db_conn = db_session.execute(
        select(DataConnection).where(DataConnection.name == "prod-orders-mysql")
    ).scalar_one()
    assert raw_password not in str(db_conn.config_non_secret_json)


def test_file_path_traversal_and_symlink_escape_blocked(tmp_path: Path) -> None:
    """验证路径穿越守卫对 ..、URL 编码 ..、宿主机绝对路径与符号链接逃逸的 100% 拦截。"""
    allowed_root = tmp_path / "controlled_uploads"
    allowed_root.mkdir(parents=True, exist_ok=True)

    malicious_paths = [
        "../secret.txt",
        "..\\Windows\\win.ini",
        "subdir/../../etc/passwd",
        "%2e%2e%2f%2e%2e%2fetc%2fshadow",
        "/etc/passwd",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "\\\\127.0.0.1\\c$\\boot.ini",
        "~/ssh_key",
        "valid_name.csv\x00.exe",
    ]
    for bad_path in malicious_paths:
        with pytest.raises(AppError) as exc_info:
            validate_safe_file_path(bad_path, allowed_root=allowed_root)
        assert exc_info.value.error_code == ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED.value

    # 测试符号链接（Symlink）逃逸拦截
    outside_file = tmp_path / "outside_host_secret.txt"
    outside_file.write_text("TOP_SECRET", encoding="utf-8")
    symlink_path = allowed_root / "escaped_link.csv"
    try:
        os.symlink(outside_file, symlink_path)
        symlink_created = True
    except OSError:
        # 若宿主机当前账户未开启符号链接权限，跳过物理 symlink 创建但仍验证正常路径
        symlink_created = False

    if symlink_created:
        with pytest.raises(AppError) as exc_info:
            validate_safe_file_path("escaped_link.csv", allowed_root=allowed_root)
        assert exc_info.value.error_code == ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED.value

    # 合法相对路径应当通过并落入 allowed_root 内部
    safe_resolved = validate_safe_file_path("tenant_a/orders_2026.csv", allowed_root=allowed_root)
    assert str(safe_resolved).startswith(str(allowed_root.resolve()))


def test_immutable_metadata_snapshots_sensitive_masking_and_schema_drift(
    api_client: TestClient,
    seeded_env: dict[str, Any],
) -> None:
    """验证元数据刷新始终创建新不可变 Snapshot、自动敏感识别与样本脱敏、以及 Schema Drift 差异计算。"""
    tokens = _complete_login_flow(api_client, "maker@etl-agent.local")
    org_id = seeded_env["organizations"]["org-alpha"].id
    proj_id = seeded_env["projects"]["proj-orders-prod"].id
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Organization-Id": str(org_id),
    }

    conn_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/connections",
        headers=headers,
        json={
            "name": "demo-orders-db",
            "connector_id": "mysql-jdbc",
            "connection_type": "MYSQL",
            "config": {"host": "127.0.0.1", "port": 3306, "database": "demo"},
        },
    )
    conn_id = conn_resp.json()["id"]

    # 第一次探查：生成 Snapshot v1（含敏感字段 customer_email, customer_phone）
    v1_objects = [
        {
            "schema_name": "orders_db",
            "object_name": "users",
            "object_type": "TABLE",
            "row_count_approx": 1000,
            "primary_key": ["user_id"],
            "columns": [
                {"column_name": "user_id", "data_type": "BIGINT", "nullable": False},
                {"column_name": "customer_email", "data_type": "VARCHAR(128)", "nullable": False},
                {"column_name": "customer_phone", "data_type": "VARCHAR(32)", "nullable": True},
                {"column_name": "legacy_code", "data_type": "VARCHAR(16)", "nullable": True},
            ],
            "sample_rows": [
                {
                    "user_id": 101,
                    "customer_email": "alice.smith@example.com",
                    "customer_phone": "13812345678",
                    "legacy_code": "LC-01",
                }
            ],
        }
    ]
    probe1_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/probe",
        headers=headers,
        json={"connector_version": "2.3.9", "override_objects": v1_objects},
    )
    assert probe1_resp.status_code == 201
    snap1 = probe1_resp.json()
    snap1_id = snap1["snapshot_id"]

    # 验证样本已脱敏（原始邮箱与手机号不直接暴露）
    masked_row = snap1["objects"][0]["masked_sample_rows"][0]
    assert masked_row["customer_email"] != "alice.smith@example.com"
    assert "***@" in masked_row["customer_email"]
    assert masked_row["customer_phone"] != "13812345678"
    assert "****" in masked_row["customer_phone"]

    # 人工标注敏感分级（PATCH /classifications）
    obj1_id = snap1["objects"][0]["object_id"]
    patch_resp = api_client.patch(
        f"/api/v1/projects/{proj_id}/metadata/snapshots/{snap1_id}/classifications",
        headers=headers,
        json={
            "object_id": obj1_id,
            "column_name": "legacy_code",
            "classification_level": "CONFIDENTIAL",
            "pii_category": "INTERNAL_CODE",
        },
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["source_mode"] == "MANUAL_MARK"
    assert patch_resp.json()["classification_level"] == "CONFIDENTIAL"

    # 第二次探查：Schema 发生变更（删除 legacy_code 列，修改 customer_phone 类型，新增 loyalty_tier 列）
    v2_objects = [
        {
            "schema_name": "orders_db",
            "object_name": "users",
            "object_type": "TABLE",
            "row_count_approx": 1200,
            "primary_key": ["user_id"],
            "columns": [
                {"column_name": "user_id", "data_type": "BIGINT", "nullable": False},
                {"column_name": "customer_email", "data_type": "VARCHAR(128)", "nullable": False},
                {"column_name": "customer_phone", "data_type": "BIGINT", "nullable": True},
                {"column_name": "loyalty_tier", "data_type": "VARCHAR(32)", "nullable": True},
            ],
            "sample_rows": [],
        }
    ]
    probe2_resp = api_client.post(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/probe",
        headers=headers,
        json={"connector_version": "2.3.9", "override_objects": v2_objects},
    )
    assert probe2_resp.status_code == 201
    snap2 = probe2_resp.json()
    snap2_id = snap2["snapshot_id"]

    # 核心不变量：必须生成全新 snapshot_id，且指纹发生变化，触发 HIGH_BREAKING SchemaDriftEvent
    assert snap2_id != snap1_id
    assert snap2["schema_fingerprint"] != snap1["schema_fingerprint"]
    assert snap2["drift_event"] is not None
    assert snap2["drift_event"]["severity"] == "HIGH_BREAKING"
    diff = snap2["drift_event"]["diff"]
    assert any(c["column_name"] == "legacy_code" for c in diff["removed_columns"])
    assert any(c["column_name"] == "loyalty_tier" for c in diff["added_columns"])
    assert any(c["column_name"] == "customer_phone" for c in diff["type_changes"])

    # 查询历史快照列表：两个快照同时不可变保留
    snaps_list = api_client.get(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/snapshots",
        headers=headers,
    ).json()["items"]
    assert len(snaps_list) == 2

    # 查询 Drift 历史列表
    drifts_list = api_client.get(
        f"/api/v1/projects/{proj_id}/connections/{conn_id}/metadata/drift",
        headers=headers,
    ).json()["items"]
    assert len(drifts_list) == 1
    assert drifts_list[0]["from_snapshot_id"] == snap1_id
    assert drifts_list[0]["to_snapshot_id"] == snap2_id
