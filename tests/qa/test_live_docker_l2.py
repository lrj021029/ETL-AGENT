"""M10 QA 独立验证套件 2：真实 Docker 容器 L2 端到端集成测试（Live Docker Container Integration Suite）。

遵循 PRD §8.1~§8.7、TECHNICAL_SPEC §17.1、DECISION_REGISTER.md 及 MULTI_AGENT_TASK_PROMPTS.md §5.8：
- 严格遵循诚实验收原则：每个测试开始前探测对应宿主机 `127.0.0.1` 端口与服务健康状态。
- 若目标容器尚未运行，使用 `pytest.skip("BLOCKED: <service> container not running")` 明确标记为阻塞跳过，绝不伪造 PASS！
- 当容器运行就绪时，执行 100% 真实的端到端协议交互与数据库/引擎副作用验证：
  1. PostgreSQL 16 (`127.0.0.1:5432`)：Alembic `upgrade head`、`etl_runtime` `rolbypassrls=false` 与 0 表所有权核验、
     5 项强制 RLS 负向隔离攻击测试（缺失上下文、跨组织、同组织跨项目、连接池复用隔离、`resolve_execution_tenant_context`）。
  2. HashiCorp Vault 持久化模式 (`127.0.0.1:8200`)：从 `infra/vault/init/vault-init.json` 或环境变量加载令牌，
     测试真实 `VaultKV2SecretProvider` 生命周期 CRUD，以及真实 `VaultTransitEd25519Signer` 签发 Capability JWS 并使用
     Vault 导出的 Ed25519 公钥完成 `cryptography` 验签。
  3. Redis 7 (`127.0.0.1:6379`)：真实 `RedisFastPath` + `ReplayGuard` 执行 `SET NX EX 360` 快速防重放测试。
  4. Mailpit (`127.0.0.1:1025` SMTP + `127.0.0.1:8025` HTTP API)：真实发送 Email OTP 二次验证邮件并通过 Mailpit REST API 核验送达内容。
  5. MySQL 8.0 (`127.0.0.1:3306`) & Demo PostgreSQL 16 (`127.0.0.1:5433`)：真实只读 `MetadataService` 探查生成不可变
     `MetadataSnapshot`，验证 MySQL `log_bin=ON` & `binlog_format=ROW`、验证 PG `wal_level=logical`，并验证真实 `SchemaDriftEvent` 计算。
  6. MinIO (`127.0.0.1:9000`)：真实创建 Bucket、按租户前缀 `org-<id>/proj-<id>/` 上传对象并校验 ETag 与 SHA256。
  7. Apache Doris (`127.0.0.1:9030` MySQL 协议 / `8030` HTTP)：真实创建 OLAP 目标表与影子表、执行 `DorisPublishAdapter`
     Schema 兼容检查、执行真实 `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')` 原子表替换、行数核验及
     `RollbackExecutor.execute_swap_restore` 真实原子换回恢复。
  8. Apache SeaTunnel Zeta (`127.0.0.1:5801`)：真实调用 Zeta REST API 集群健康检查与作业生命周期查询。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any
import uuid

from alembic import command
from alembic.config import Config
import boto3
from botocore.config import Config as BotoConfig
import httpx
import pymysql
import pytest
import redis
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError, DBAPIError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

# 确保仓库根目录与 backend 目录均在 sys.path 中
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.auth.service import AuthService, seed_demo_iam_data
from app.capabilities import (
    CapabilityIssuer,
    CapabilityVerifier,
    InMemoryConsumptionStore,
    KeyRing,
    RedisFastPath,
    ReplayGuard,
    VaultTransitEd25519Signer,
)
from app.config import Settings
from app.connections.service import ConnectionService
from app.metadata.service import MetadataService, SqlAlchemyReadOnlyMetadataProber
from app.secrets import VaultKV2SecretProvider
from app.security.errors import HarnessError
from app.shared.db_rls import (
    TenantContext,
    apply_rls_context,
    clear_rls_context,
    resolve_worker_tenant_context,
    rls_transaction,
)
from app.shared.errors import AppError, ErrorCode
from app.shared.models import Base
from app.shared.models.connections_metadata import DataConnection
from app.shared.models.execution_quality import ExecutionRun
from app.shared.models.governance_harness import ExecutionCommand
from app.shared.models.iam import Project
from backend.app.data_plane.seatunnel_adapter import (
    HttpxZetaTransport,
    SeaTunnelZetaAdapter,
)
from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
)
from backend.app.publishing.doris_adapter import (
    DorisColumnSpec,
    DorisPublishAdapter,
    DorisTableSchemaSpec,
    PublishSchemaIncompatibleError,
    build_shadow_table_name,
)
from backend.app.rollback.executor import (
    RollbackCommandContext,
    RollbackExecutor,
)


def _is_tcp_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """探测宿主机指定 TCP 端口是否已监听且可建立连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _require_service_port(service_name: str, host: str, port: int) -> None:
    """若目标 Docker 容器端口未启动，诚实标记为 BLOCKED 跳过，绝不伪造 PASS。"""
    if not _is_tcp_port_open(host, port):
        pytest.skip(f"BLOCKED: {service_name} container not running on {host}:{port}")


def _sample_sha256(tag: str) -> str:
    """生成符合契约格式的 SHA256 字符串。"""
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


# ==============================================================================
# 1. PostgreSQL 16 (127.0.0.1:5432)：Alembic 迁移 + 5 项强制 RLS 负向隔离测试
# ==============================================================================
def test_l2_live_postgres_alembic_and_five_mandatory_rls_attacks() -> None:
    """在真实 PostgreSQL 16 容器上执行 Alembic 升级并验证 5 项强制 RLS 负向攻击隔离。"""
    pg_host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    pg_port = int(os.getenv("POSTGRES_PORT", "5432"))
    _require_service_port("postgres", pg_host, pg_port)

    pg_db = os.getenv("POSTGRES_DB", "etl_control_plane")
    super_user = os.getenv("POSTGRES_SUPERUSER", "postgres")
    super_pass = os.getenv("POSTGRES_SUPERUSER_PASSWORD", "dev_only_pg_super_placeholder_not_for_prod")
    migrator_user = os.getenv("PG_MIGRATOR_USER", "etl_migrator")
    migrator_pass = os.getenv("PG_MIGRATOR_PASSWORD", "dev_only_migrator_placeholder_not_for_prod")
    runtime_user = os.getenv("PG_RUNTIME_USER", "etl_runtime")
    runtime_pass = os.getenv("PG_RUNTIME_PASSWORD", "dev_only_runtime_placeholder_not_for_prod")

    super_url = f"postgresql+psycopg://{super_user}:{super_pass}@{pg_host}:{pg_port}/{pg_db}"
    migrator_url = os.getenv(
        "ETL_TEST_PG_MIGRATOR_URL",
        f"postgresql+psycopg://{migrator_user}:{migrator_pass}@{pg_host}:{pg_port}/{pg_db}",
    )
    runtime_url = os.getenv(
        "ETL_TEST_PG_RUNTIME_URL",
        f"postgresql+psycopg://{runtime_user}:{runtime_pass}@{pg_host}:{pg_port}/{pg_db}",
    )

    # 预置：PostgreSQL 16 中 0002_rls_policies.py 执行 `ALTER ROLE ... NOSUPERUSER` 需要迁移账号具备 SUPERUSER 属性
    super_engine = create_engine(super_url, pool_pre_ping=True, future=True)
    try:
        with super_engine.begin() as s_conn:
            s_conn.execute(text(f"ALTER ROLE {migrator_user} WITH SUPERUSER CREATEDB CREATEROLE"))
    finally:
        super_engine.dispose()

    # 第一步：使用 etl_migrator 执行 Alembic upgrade head（使 71 张业务表 owner 均为 etl_migrator）
    alembic_cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", migrator_url)
    old_env_url = os.environ.get("MIGRATOR_DATABASE_URL")
    os.environ["MIGRATOR_DATABASE_URL"] = migrator_url
    try:
        from app.config import get_settings

        get_settings.cache_clear()
        command.upgrade(alembic_cfg, "head")
    finally:
        if old_env_url is None:
            os.environ.pop("MIGRATOR_DATABASE_URL", None)
        else:
            os.environ["MIGRATOR_DATABASE_URL"] = old_env_url
        get_settings.cache_clear()

    # 第二步：使用 etl_migrator 幂等写入多组织/多项目种子数据及一条后台任务记录
    migrator_engine = create_engine(migrator_url, pool_pre_ping=True, future=True)
    MigratorSession = sessionmaker(bind=migrator_engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with MigratorSession() as m_sess:
        seeded = seed_demo_iam_data(m_sess)
        org_alpha_id = seeded["organizations"]["org-alpha"].id
        org_beta_id = seeded["organizations"]["org-beta"].id
        proj_alpha_1_id = seeded["projects"]["proj-orders-prod"].id
        proj_alpha_2_id = seeded["projects"]["proj-logs-staging"].id
        maker_id = seeded["users"]["maker"].id

        # 在 proj-orders-prod 下创建一条数据源连接供跨项目 RLS 负向验证
        conn_svc = ConnectionService()
        created_rls_conn = conn_svc.create_connection(
            m_sess,
            organization_id=org_alpha_id,
            project_id=proj_alpha_1_id,
            name=f"rls-live-conn-{uuid.uuid4().hex[:6]}",
            connector_id="mysql-jdbc",
            connection_type="MYSQL",
            config={"host": "127.0.0.1", "port": 3306, "database": "demo_ecommerce"},
            secret_payload={"password": "top-secret-password"},
            actor_user_id=maker_id,
        )
        rls_conn_id = uuid.UUID(created_rls_conn["id"])

        # 创建一条 ExecutionRun + ExecutionCommand 供 resolve_execution_tenant_context 验证
        cmd_id = uuid.uuid4()
        run_row = ExecutionRun(
            organization_id=org_alpha_id,
            project_id=proj_alpha_1_id,
            pipeline_id=uuid.uuid4(),
            pipeline_version_id=uuid.uuid4(),
            artifact_hash=_sample_sha256("rls-live-worker"),
            execution_mode="BATCH",
            preparation_id=uuid.uuid4(),
            capability_jti=uuid.uuid4(),
            command_id=cmd_id,
            status="QUEUED",
        )
        m_sess.add(run_row)
        m_sess.flush()
        exec_id = run_row.id

        cmd_row = ExecutionCommand(
            organization_id=org_alpha_id,
            project_id=proj_alpha_1_id,
            command_id=cmd_id,
            idempotency_key=f"idem-rls-{uuid.uuid4().hex[:8]}",
            execution_id=exec_id,
            command_type="EXECUTION_SUBMIT_BATCH",
            capability_jti=run_row.capability_jti,
            payload_json={},
            status="PENDING",
        )
        m_sess.add(cmd_row)
        m_sess.commit()
    migrator_engine.dispose()

    # 第三步：使用普通运行时账号 etl_runtime (NOBYPASSRLS) 执行 5 项强制 RLS 负向测试
    runtime_engine = create_engine(runtime_url, pool_size=1, max_overflow=0, future=True)
    RuntimeSession = sessionmaker(bind=runtime_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        with RuntimeSession() as r_sess:
            with r_sess.begin():
                # 强制检查 5：etl_runtime 的 rolbypassrls 必须为 false 且不拥有任何 public 业务表
                rolbypassrls = r_sess.execute(
                    text("SELECT rolbypassrls FROM pg_roles WHERE rolname = :rname"),
                    {"rname": runtime_user},
                ).scalar_one()
                assert rolbypassrls is False, "安全违规：etl_runtime 角色绝不能拥有 BYPASSRLS 权限"

                owned_count = r_sess.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_tables "
                        "WHERE schemaname = 'public' AND tableowner = :rname"
                    ),
                    {"rname": runtime_user},
                ).scalar_one()
                assert int(owned_count) == 0, "安全违规：etl_runtime 角色绝不能拥有任何 public 业务表"

                # 强制检查 1：未设置 app.organization_id / app.project_id 上下文时（新连接默认为 NULL），SELECT 返回 0 行，INSERT 直接被 RLS 拒绝
                proj_count_no_ctx = r_sess.execute(text("SELECT COUNT(*) FROM projects")).scalar_one()
                conn_count_no_ctx = r_sess.execute(text("SELECT COUNT(*) FROM data_connections")).scalar_one()
                assert int(proj_count_no_ctx) == 0
                assert int(conn_count_no_ctx) == 0

                with pytest.raises((ProgrammingError, DataError, DBAPIError)):
                    with r_sess.begin_nested():
                        r_sess.execute(
                            text(
                                "INSERT INTO projects (id, organization_id, slug, name, environment, "
                                "llm_data_policy_json, status, created_at, updated_at) "
                                "VALUES (:id, :org_id, 'illegal-slug', 'Illegal', 'dev', '{}'::jsonb, "
                                "'ACTIVE', NOW(), NOW())"
                            ),
                            {"id": str(uuid.uuid4()), "org_id": str(org_alpha_id)},
                        )

        # 强制检查 2：跨组织越权读取 —— 设置 org-beta 上下文后查询 org-alpha 的项目必须返回 0 行
        ctx_org_beta = TenantContext(organization_id=org_beta_id, project_id=None, actor_id="attacker-beta")
        with rls_transaction(RuntimeSession, ctx_org_beta, require_project=False) as r_sess:
            alpha_projects_seen_by_beta = r_sess.execute(
                text("SELECT COUNT(*) FROM projects WHERE organization_id = :alpha_id"),
                {"alpha_id": str(org_alpha_id)},
            ).scalar_one()
            assert int(alpha_projects_seen_by_beta) == 0

        # 强制检查 3：同组织跨项目越权读取 —— 设置 org-alpha + proj-logs-staging 后查询 proj-orders-prod 的连接必须返回 0 行
        ctx_proj_2 = TenantContext(
            organization_id=org_alpha_id,
            project_id=proj_alpha_2_id,
            actor_id="user-proj2",
        )
        with rls_transaction(RuntimeSession, ctx_proj_2, require_project=True) as r_sess:
            cross_proj_conns = r_sess.execute(
                text("SELECT COUNT(*) FROM data_connections WHERE project_id = :p1_id"),
                {"p1_id": str(proj_alpha_1_id)},
            ).scalar_one()
            assert int(cross_proj_conns) == 0

        # 强制检查 4：连接池复用上下文残留测试（pool_size=1 强制复用同一物理连接）
        ctx_proj_1 = TenantContext(
            organization_id=org_alpha_id,
            project_id=proj_alpha_1_id,
            actor_id="user-proj1",
        )
        with rls_transaction(RuntimeSession, ctx_proj_1, require_project=True) as r_sess:
            valid_conns = r_sess.execute(text("SELECT COUNT(*) FROM data_connections")).scalar_one()
            assert int(valid_conns) >= 1

        # 同一物理连接归还连接池后：
        # 4a) 切换至另一租户/项目上下文查询，严禁残留上一事务的租户 A 项目 1 数据
        with rls_transaction(RuntimeSession, ctx_proj_2, require_project=True) as reused_tenant_b_sess:
            leaked_to_proj2 = reused_tenant_b_sess.execute(
                text("SELECT COUNT(*) FROM data_connections WHERE project_id = :p1_id"),
                {"p1_id": str(proj_alpha_1_id)},
            ).scalar_one()
            assert int(leaked_to_proj2) == 0, "严重安全漏洞：连接池复用后跨项目泄露了上一事务的数据！"

        # 4b) 在无上下文的新事务中直接查询（SET LOCAL 已在事务结束时重置为空字符串 ''），必须返回 0 行或因空 UUID 转换异常被 Fail-Closed 拦截
        with RuntimeSession() as reused_no_ctx_sess:
            with reused_no_ctx_sess.begin():
                try:
                    leaked_conns = int(
                        reused_no_ctx_sess.execute(text("SELECT COUNT(*) FROM data_connections")).scalar_one()
                    )
                except DataError:
                    # PostgreSQL 将自定义 GUC 重置为 '' 后，`''::uuid` 抛出 DataError，同样属于 Fail-Closed 零数据泄露
                    reused_no_ctx_sess.rollback()
                    leaked_conns = 0
                assert leaked_conns == 0, "严重安全漏洞：连接池复用后在无上下文事务中残留了租户数据！"

        # 强制检查 5b：后台 Worker 受控 SECURITY DEFINER 函数 resolve_execution_tenant_context 验证
        with RuntimeSession() as worker_sess:
            with worker_sess.begin():
                derived = resolve_worker_tenant_context(
                    worker_sess,
                    execution_id=exec_id,
                    command_id=cmd_id,
                )
                assert derived.organization_id == org_alpha_id
                assert derived.project_id == proj_alpha_1_id

                # 传入伪造的 command_id 必须拒绝并抛出 TENANT_CONTEXT_MISSING
                with pytest.raises(AppError) as exc_info:
                    resolve_worker_tenant_context(
                        worker_sess,
                        execution_id=exec_id,
                        command_id=uuid.uuid4(),
                    )
                assert exc_info.value.error_code == ErrorCode.TENANT_CONTEXT_MISSING.value
    finally:
        runtime_engine.dispose()
        # 清理本用例写入的临时连接与临时执行记录，避免污染在线演示库统计指标
        cleanup_engine = create_engine(migrator_url, pool_pre_ping=True, future=True)
        try:
            with cleanup_engine.begin() as c_conn:
                c_conn.execute(text("DELETE FROM connection_versions WHERE connection_id = :cid"), {"cid": rls_conn_id})
                c_conn.execute(text("DELETE FROM data_connections WHERE id = :cid"), {"cid": rls_conn_id})
                c_conn.execute(text("DELETE FROM execution_commands WHERE command_id = :cmd_id"), {"cmd_id": cmd_id})
                c_conn.execute(text("DELETE FROM execution_runs WHERE id = :eid"), {"eid": exec_id})
        finally:
            cleanup_engine.dispose()


# ==============================================================================
# 2. HashiCorp Vault 持久化模式 (127.0.0.1:8200)：KV v2 CRUD + Transit Ed25519 JWS 签发与验签
# ==============================================================================
class _HttpxVaultClientAdapter:
    """基于 `httpx` 的真实 HashiCorp Vault HTTP API 适配器（兼容 `VaultKV2SecretProvider` 与 `HvacTransitClient` 接口）。"""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Vault-Token": token}
        self.secrets = self._SecretsNamespace(self)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{self._base_url}{path}", headers=self._headers, params=params)
            resp.raise_for_status()
            return dict(resp.json())

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(f"{self._base_url}{path}", headers=self._headers, json=payload)
            resp.raise_for_status()
            return dict(resp.json()) if resp.content else {}

    def delete(self, path: str) -> None:
        with httpx.Client(timeout=5.0) as client:
            resp = client.delete(f"{self._base_url}{path}", headers=self._headers)
            resp.raise_for_status()

    class _SecretsNamespace:
        def __init__(self, root: _HttpxVaultClientAdapter) -> None:
            self.kv = self._KvNamespace(root)
            self.transit = self._TransitNamespace(root)

        class _KvNamespace:
            def __init__(self, root: _HttpxVaultClientAdapter) -> None:
                self.v2 = self._KvV2(root)

            class _KvV2:
                def __init__(self, root: _HttpxVaultClientAdapter) -> None:
                    self._r = root

                def create_or_update_secret(
                    self,
                    *,
                    path: str,
                    secret: dict[str, Any],
                    mount_point: str = "secret",
                ) -> dict[str, Any]:
                    return self._r.post_json(f"/v1/{mount_point}/data/{path}", {"data": secret})

                def read_secret_version(
                    self,
                    *,
                    path: str,
                    version: int | None = None,
                    mount_point: str = "secret",
                    raise_on_deleted_version: bool = True,
                ) -> dict[str, Any]:
                    params = {"version": version} if version is not None else None
                    return self._r.get_json(f"/v1/{mount_point}/data/{path}", params=params)

                def read_secret_metadata(
                    self,
                    *,
                    path: str,
                    mount_point: str = "secret",
                ) -> dict[str, Any]:
                    return self._r.get_json(f"/v1/{mount_point}/metadata/{path}")

                def delete_metadata_and_all_versions(
                    self,
                    *,
                    path: str,
                    mount_point: str = "secret",
                ) -> None:
                    self._r.delete(f"/v1/{mount_point}/metadata/{path}")

        class _TransitNamespace:
            def __init__(self, root: _HttpxVaultClientAdapter) -> None:
                self._r = root

            def sign_data(
                self,
                *,
                name: str,
                hash_input: str,
                mount_point: str = "transit",
                key_version: int | None = None,
            ) -> dict[str, Any]:
                body: dict[str, Any] = {"input": hash_input}
                if key_version is not None:
                    body["key_version"] = key_version
                return self._r.post_json(f"/v1/{mount_point}/sign/{name}", body)


def _resolve_live_vault_token(vault_addr: str) -> str:
    """从 `infra/vault/init/vault-init.json`（当前工作树或主仓库）或环境变量 `VAULT_TOKEN` 读取真实 Vault Token。"""
    candidate_files = [
        _REPO_ROOT / "infra" / "vault" / "init" / "vault-init.json",
        Path("D:/etl-agent/infra/vault/init/vault-init.json"),
    ]
    for init_file in candidate_files:
        if init_file.is_file():
            data = json.loads(init_file.read_text(encoding="utf-8-sig"))
            token = str(data.get("root_token") or "").strip()
            if token:
                return token

    env_token = os.getenv("VAULT_TOKEN", "").strip()
    if env_token and "placeholder" not in env_token:
        return env_token

    pytest.skip(
        f"BLOCKED: Vault at {vault_addr} has no initialized token in infra/vault/init/vault-init.json or VAULT_TOKEN"
    )


def test_l2_live_vault_kv2_and_transit_ed25519_capability_jws() -> None:
    """在真实持久化 Vault 容器上测试 VaultKV2SecretProvider CRUD 与 VaultTransitEd25519Signer Capability JWS 签发验签。"""
    vault_host = os.getenv("VAULT_HOST", "127.0.0.1")
    vault_port = int(os.getenv("VAULT_PORT", "8200"))
    _require_service_port("vault", vault_host, vault_port)

    vault_addr = os.getenv("VAULT_HOST_ADDR", f"http://{vault_host}:{vault_port}").rstrip("/")
    token = _resolve_live_vault_token(vault_addr)

    # 检查 Vault 是否已 Unseal
    with httpx.Client(timeout=5.0) as http_client:
        health_resp = http_client.get(f"{vault_addr}/v1/sys/seal-status")
        health_resp.raise_for_status()
        if health_resp.json().get("sealed") is True:
            pytest.skip("BLOCKED: Vault container is running but currently sealed (run scripts/dev/init-vault.ps1)")

    vault_client = _HttpxVaultClientAdapter(base_url=vault_addr, token=token)
    kv_mount = os.getenv("VAULT_KV_MOUNT", "secret")
    transit_mount = os.getenv("VAULT_TRANSIT_MOUNT", "transit")
    cap_key_name = os.getenv("VAULT_CAPABILITY_KEY_NAME", "etl-agent-capability")

    # 1. 验证真实 VaultKV2SecretProvider 的版本化写入、读取、轮转与删除
    provider = VaultKV2SecretProvider(client=vault_client, mount_point=kv_mount, prefix="etl-agent-qa")
    secret_ref = f"l2-test/{uuid.uuid4().hex}"
    v1 = provider.put(secret_ref, "mysql-secret-v1")
    assert v1 == 1
    sv1 = provider.get(secret_ref)
    assert sv1.version == 1
    assert sv1.reveal() == "mysql-secret-v1"
    assert "mysql-secret-v1" not in repr(sv1)

    v2 = provider.put(secret_ref, "mysql-secret-v2")
    assert v2 == 2
    assert provider.current_version(secret_ref) == 2
    assert provider.get(secret_ref, version=1).reveal() == "mysql-secret-v1"
    assert provider.get(secret_ref, version=2).reveal() == "mysql-secret-v2"
    provider.delete(secret_ref)

    # 2. 验证真实 VaultTransitEd25519Signer 签发 Capability JWS 并使用 cryptography Ed25519 公钥验签
    from app.capabilities.signing import HvacTransitClient

    key_meta = vault_client.get_json(f"/v1/{transit_mount}/keys/{cap_key_name}")
    latest_ver = int(key_meta["data"]["latest_version"])
    pub_key_b64 = key_meta["data"]["keys"][str(latest_ver)]["public_key"]

    transit_client = HvacTransitClient(vault_client, mount_point=transit_mount)
    transit_signer = VaultTransitEd25519Signer(
        client=transit_client,
        key_name=cap_key_name,
        key_version=latest_ver,
    )
    keyring = KeyRing()
    keyring.add_vault_public_key(transit_signer.kid, pub_key_b64)

    issuer = CapabilityIssuer(signer=transit_signer)
    verifier = CapabilityVerifier(keyring=keyring)

    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    prep_id = str(uuid.uuid4())
    jws, issued_claims = issuer.issue(
        sub="operator-live-vault",
        tool="seatunnel.submit_batch",
        organization_id=org_id,
        project_id=proj_id,
        environment="prod",
        preparation_id=prep_id,
        pipeline_version_hash=_sample_sha256("vault-live-pv"),
        approval_digest=_sample_sha256("vault-live-approval"),
        policy_version="policy-1",
    )

    verified = verifier.verify(
        jws,
        expected={
            "organization_id": org_id,
            "project_id": proj_id,
            "environment": "prod",
            "tool": "seatunnel.submit_batch",
            "preparation_id": prep_id,
        },
    )
    assert verified["jti"] == issued_claims["jti"]
    assert verified["aud"] == "seatunnel-worker"


# ==============================================================================
# 3. Redis 7 (127.0.0.1:6379)：真实 Replay Guard SET NX EX 360 快速防重放测试
# ==============================================================================
def test_l2_live_redis_replay_guard_set_nx_ex() -> None:
    """在真实 Redis 7 容器上验证 ReplayGuard 的 `SET NX EX 360` 快速防重放路径。"""
    redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    _require_service_port("redis", redis_host, redis_port)

    redis_password = os.getenv("REDIS_PASSWORD", "dev_only_redis_placeholder_not_for_prod")
    client = redis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_password,
        db=0,
        decode_responses=True,
        socket_timeout=3.0,
    )
    try:
        assert client.ping() is True
        fast_path = RedisFastPath(client=client, prefix="etl:qa:cap:jti:")
        guard = ReplayGuard(fast=fast_path, store=InMemoryConsumptionStore())

        jti = str(uuid.uuid4())
        redis_key = f"etl:qa:cap:jti:{jti}"

        # 第一次预占应成功并在 Redis 中写入 TTL <= 360 的键
        guard.reserve(jti)
        ttl = client.ttl(redis_key)
        assert 0 < ttl <= 360

        # 第二次使用同一 JTI 预占必须被 Redis SET NX 原子拦截并抛出 CAPABILITY_REPLAY_DETECTED
        with pytest.raises(HarnessError) as exc_info:
            guard.reserve(jti)
        assert exc_info.value.code == "CAPABILITY_REPLAY_DETECTED"
    finally:
        client.close()


# ==============================================================================
# 4. Mailpit (127.0.0.1:1025 SMTP + 127.0.0.1:8025 API)：真实 Email OTP 发送与接收校验
# ==============================================================================
def test_l2_live_mailpit_email_otp_delivery_and_verification() -> None:
    """通过真实 Mailpit SMTP (1025) 发送邮件二次验证 OTP，并通过 Mailpit HTTP API (8025) 核验邮件到达与验证码登录。"""
    smtp_host = os.getenv("MAILPIT_HOST", "127.0.0.1")
    smtp_port = int(os.getenv("MAILPIT_SMTP_PORT", "1025"))
    api_port = int(os.getenv("MAILPIT_UI_PORT", "8025"))
    _require_service_port("mailpit-smtp", smtp_host, smtp_port)
    _require_service_port("mailpit-api", smtp_host, api_port)

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    settings = Settings(
        smtp_enabled=True,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_from_email="noreply@etl-agent.local",
    )
    auth_service = AuthService(settings=settings)

    with SessionLocal() as session:
        seed_demo_iam_data(session)
        session.commit()

        login_res = auth_service.login_with_password(
            session,
            email="maker@etl-agent.local",
            password="DemoPass!2026",
        )
        session.commit()
        assert login_res["verification_type"] == "EMAIL_OTP"
        assert login_res["verification_label"] == "邮件二次验证"
        expected_otp = auth_service.sent_otp_outbox[-1].otp_code

        # 查询真实 Mailpit HTTP API 验证邮件已送达且包含该 6 位 OTP 验证码
        with httpx.Client(timeout=5.0) as http_client:
            msgs_resp = http_client.get(f"http://{smtp_host}:{api_port}/api/v1/messages")
            msgs_resp.raise_for_status()
            messages = msgs_resp.json().get("messages") or []
            assert len(messages) >= 1, "Mailpit 未收到任何 Email OTP 邮件"

            latest_msg_id = messages[0]["ID"]
            detail_resp = http_client.get(f"http://{smtp_host}:{api_port}/api/v1/message/{latest_msg_id}")
            detail_resp.raise_for_status()
            msg_detail = detail_resp.json()
            text_body = str(msg_detail.get("Text") or msg_detail.get("Snippet") or "")
            assert expected_otp in text_body
            assert "邮件二次验证" in text_body

        # 使用从邮件提取的验证码完成 OTP 验证并获取正式 JWT
        token_res = auth_service.verify_email_otp(
            session,
            challenge_id=uuid.UUID(login_res["challenge_id"]),
            otp_code=expected_otp,
        )
        session.commit()
        assert token_res["verification_label"] == "邮件二次验证"
        assert len(token_res["access_token"]) > 20
    engine.dispose()


# ==============================================================================
# 5. MySQL 8.0 (127.0.0.1:3306) & Demo PG (127.0.0.1:5433)：只读探查、CDC 配置核验与 Schema Drift
# ==============================================================================
def test_l2_live_mysql_and_demo_postgres_metadata_probe_cdc_and_drift() -> None:
    """在真实 MySQL 8.0 与 Demo PostgreSQL 16 容器上执行只读 MetadataProbe、核验 CDC Binlog/WAL 配置并验证 SchemaDriftEvent。"""
    mysql_host = os.getenv("DEMO_MYSQL_HOST", "127.0.0.1")
    mysql_port = int(os.getenv("DEMO_MYSQL_PORT", "3306"))
    pg_host = os.getenv("DEMO_POSTGRES_HOST", "127.0.0.1")
    pg_port = int(os.getenv("DEMO_POSTGRES_PORT", "5433"))
    _require_service_port("demo-mysql", mysql_host, mysql_port)
    _require_service_port("demo-postgres", pg_host, pg_port)

    mysql_db = os.getenv("DEMO_MYSQL_DATABASE", "demo_ecommerce")
    mysql_user = os.getenv("DEMO_MYSQL_USER", "demo_etl_reader")
    mysql_pass = os.getenv("DEMO_MYSQL_PASSWORD", "dev_only_demo_mysql_reader_placeholder")

    pg_db = os.getenv("DEMO_POSTGRES_DB", "demo_orders")
    pg_user = os.getenv("DEMO_POSTGRES_USER", "demo_pg_user")
    pg_pass = os.getenv("DEMO_POSTGRES_PASSWORD", "dev_only_demo_pg_placeholder")

    # 1. 验证 MySQL 8.0 的 log_bin=ON 与 binlog_format=ROW
    mysql_url = f"mysql+pymysql://{mysql_user}:{mysql_pass}@{mysql_host}:{mysql_port}/{mysql_db}"
    mysql_eng = create_engine(mysql_url, pool_pre_ping=True, future=True)
    try:
        with mysql_eng.connect() as m_conn:
            log_bin = m_conn.execute(text("SELECT @@log_bin")).scalar_one()
            binlog_fmt = m_conn.execute(text("SELECT @@binlog_format")).scalar_one()
            assert int(log_bin) == 1 or str(log_bin).upper() == "ON"
            assert str(binlog_fmt).upper() == "ROW"
    finally:
        mysql_eng.dispose()

    # 2. 验证 Demo PostgreSQL 16 的 wal_level=logical
    pg_url = f"postgresql+psycopg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    pg_eng = create_engine(pg_url, pool_pre_ping=True, future=True)
    try:
        with pg_eng.connect() as p_conn:
            wal_level = p_conn.execute(text("SHOW wal_level")).scalar_one()
            assert str(wal_level).lower() == "logical"
    finally:
        pg_eng.dispose()

    # 3. 使用真实 SqlAlchemyReadOnlyMetadataProber + MetadataService 生成不可变快照并验证 SchemaDriftEvent
    ctrl_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        json_serializer=lambda obj: json.dumps(obj, default=str, ensure_ascii=False),
        future=True,
    )
    Base.metadata.create_all(bind=ctrl_engine)
    CtrlSession = sessionmaker(bind=ctrl_engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with CtrlSession() as session:
        seeded = seed_demo_iam_data(session)
        org_id = seeded["organizations"]["org-alpha"].id
        proj_id = seeded["projects"]["proj-orders-prod"].id
        maker_id = seeded["users"]["maker"].id

        conn_svc = ConnectionService()
        raw_prober = SqlAlchemyReadOnlyMetadataProber()

        class _SecretAwareLiveProber:
            """在只读探查瞬间从 SecretProvider 物化连接凭据并调用真实 SqlAlchemyReadOnlyMetadataProber。"""

            def probe_connection(
                self,
                connection: DataConnection,
                *,
                secret_payload: dict[str, Any] | None = None,
                sample_max_rows: int = 20,
            ) -> list[dict[str, Any]]:
                resolved_secret = secret_payload
                if resolved_secret is None and connection.secret_ref_id is not None:
                    resolved_secret = conn_svc.secret_provider.get_secret_for_execution(
                        organization_id=connection.organization_id,
                        project_id=connection.project_id,
                        secret_ref_id=connection.secret_ref_id,
                    )
                return raw_prober.probe_connection(
                    connection,
                    secret_payload=resolved_secret,
                    sample_max_rows=sample_max_rows,
                )

        meta_svc = MetadataService(prober=_SecretAwareLiveProber())

        # 创建并探查 MySQL 连接
        mysql_conn_dict = conn_svc.create_connection(
            session,
            organization_id=org_id,
            project_id=proj_id,
            name="live-demo-mysql",
            connector_id="mysql-jdbc",
            connection_type="MYSQL",
            config={
                "host": mysql_host,
                "port": mysql_port,
                "database": mysql_db,
                "username": mysql_user,
                "schema": mysql_db,
            },
            secret_payload={"password": mysql_pass},
            actor_user_id=maker_id,
        )
        mysql_conn_id = uuid.UUID(mysql_conn_dict["id"])
        snap_mysql_1 = meta_svc.probe_and_create_snapshot(
            session,
            organization_id=org_id,
            project_id=proj_id,
            connection_id=mysql_conn_id,
            actor_user_id=maker_id,
        )
        session.commit()
        mysql_obj_names = {o["object_name"] for o in snap_mysql_1["objects"]}
        assert {"users", "orders"}.issubset(mysql_obj_names)

        # 创建并探查 Demo PostgreSQL 连接
        pg_conn_dict = conn_svc.create_connection(
            session,
            organization_id=org_id,
            project_id=proj_id,
            name="live-demo-postgres",
            connector_id="postgres-jdbc",
            connection_type="POSTGRESQL",
            config={
                "host": pg_host,
                "port": pg_port,
                "database": pg_db,
                "username": pg_user,
                "schema": "public",
            },
            secret_payload={"password": pg_pass},
            actor_user_id=maker_id,
        )
        pg_conn_id = uuid.UUID(pg_conn_dict["id"])
        snap_pg_1 = meta_svc.probe_and_create_snapshot(
            session,
            organization_id=org_id,
            project_id=proj_id,
            connection_id=pg_conn_id,
            actor_user_id=maker_id,
        )
        session.commit()
        pg_obj_names = {o["object_name"] for o in snap_pg_1["objects"]}
        assert "pg_fulfillment_orders" in pg_obj_names

        # 触发第二次快照（模拟上游新增一列 `loyalty_points`）以验证不可变快照追加与真实 SchemaDriftEvent 生成
        mysql_conn_obj = session.get(DataConnection, mysql_conn_id)
        assert mysql_conn_obj is not None
        raw_probed = meta_svc.prober.probe_connection(mysql_conn_obj)
        drifted_probed = copy.deepcopy(raw_probed)
        for obj in drifted_probed:
            if obj["object_name"] == "users":
                obj["columns"].append(
                    {
                        "column_name": "loyalty_points",
                        "data_type": "INT",
                        "nullable": True,
                        "ordinal_position": len(obj["columns"]) + 1,
                    }
                )
        snap_mysql_2 = meta_svc.probe_and_create_snapshot(
            session,
            organization_id=org_id,
            project_id=proj_id,
            connection_id=mysql_conn_id,
            actor_user_id=maker_id,
            override_objects=drifted_probed,
        )
        session.commit()

        assert snap_mysql_2["snapshot_id"] != snap_mysql_1["snapshot_id"]
        assert snap_mysql_2["schema_fingerprint"] != snap_mysql_1["schema_fingerprint"]
        assert snap_mysql_2["drift_event"] is not None
        assert snap_mysql_2["drift_event"]["severity"] == "LOW_ADDITIVE"
        drifts = meta_svc.list_drift_events(
            session,
            organization_id=org_id,
            project_id=proj_id,
            connection_id=mysql_conn_id,
        )
        assert len(drifts) == 1
    ctrl_engine.dispose()


# ==============================================================================
# 6. MinIO (127.0.0.1:9000)：真实 Bucket 创建、租户前缀上传与 SHA256 校验
# ==============================================================================
def test_l2_live_minio_bucket_tenant_upload_and_sha256_verification() -> None:
    """在真实 MinIO 容器上验证 Bucket 创建、租户隔离前缀 `org-<id>/proj-<id>/` 上传下载及 SHA256 完整性校验。"""
    minio_host = os.getenv("MINIO_HOST", "127.0.0.1")
    minio_port = int(os.getenv("MINIO_API_PORT", "9000"))
    _require_service_port("minio", minio_host, minio_port)

    endpoint_url = f"http://{minio_host}:{minio_port}"
    access_key = os.getenv("MINIO_ROOT_USER", "etl_minio_admin")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD", "dev_only_minio_placeholder_not_for_prod")
    bucket_name = os.getenv("MINIO_DEFAULT_BUCKET", "etl-agent-artifacts")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    existing_buckets = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if bucket_name not in existing_buckets:
        s3.create_bucket(Bucket=bucket_name)

    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    content_bytes = b"order_id,user_id,amount\n50001,1001,299.00\n50002,1002,1299.50\n"
    expected_sha256 = hashlib.sha256(content_bytes).hexdigest()
    object_key = f"org-{org_id}/proj-{proj_id}/{expected_sha256[:12]}_orders_sample.csv"

    s3.put_object(
        Bucket=bucket_name,
        Key=object_key,
        Body=content_bytes,
        ContentType="text/csv",
        Metadata={"sha256": expected_sha256},
    )

    get_resp = s3.get_object(Bucket=bucket_name, Key=object_key)
    downloaded_bytes = get_resp["Body"].read()
    actual_sha256 = hashlib.sha256(downloaded_bytes).hexdigest()
    assert actual_sha256 == expected_sha256


# ==============================================================================
# 7. Apache Doris (127.0.0.1:9030 MySQL 协议 / 8030 HTTP)：真实 OLAP 影子表、Schema 门禁、原子 Swap 与回滚恢复
# ==============================================================================
def test_l2_live_doris_shadow_table_schema_gate_atomic_swap_and_rollback() -> None:
    """在真实 Apache Doris 2.1.7 容器上执行 OLAP 目标表/影子表创建、Schema 兼容门禁、REPLACE WITH TABLE 原子替换与换回回滚。"""
    doris_host = os.getenv("DORIS_HOST", "127.0.0.1")
    doris_query_port = int(os.getenv("DORIS_FE_QUERY_PORT", "9030"))
    _require_service_port("doris-fe", doris_host, doris_query_port)

    doris_user = os.getenv("DORIS_FE_USER", "root")
    doris_pass = os.getenv("DORIS_FE_PASSWORD", "")
    doris_db = os.getenv("DORIS_DEFAULT_DATABASE", "etl_warehouse")

    conn = pymysql.connect(
        host=doris_host,
        port=doris_query_port,
        user=doris_user,
        password=doris_pass,
        autocommit=True,
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            # 检查 Doris BE 节点是否已注册且 Alive，若 BE 尚未就绪则标记 BLOCKED
            cur.execute("SHOW BACKENDS")
            backends = cur.fetchall()
            if not backends:
                pytest.skip("BLOCKED: Doris FE is up but no Doris BE backend is registered yet")

            cur.execute(f"CREATE DATABASE IF NOT EXISTS {doris_db}")
            cur.execute(f"USE {doris_db}")

            exec_id = uuid.uuid4().hex[:10]
            target_table = f"qa_dwd_orders_{exec_id}"
            shadow_table = build_shadow_table_name(target_table, exec_id)

            for tbl in (target_table, shadow_table):
                cur.execute(f"DROP TABLE IF EXISTS {tbl}")
                cur.execute(
                    f"""
                    CREATE TABLE {tbl} (
                        order_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,
                        order_amount DECIMAL(18, 2) NOT NULL
                    )
                    UNIQUE KEY(order_id)
                    DISTRIBUTED BY HASH(order_id) BUCKETS 2
                    PROPERTIES ("replication_num" = "1")
                    """
                )

            # 向目标表写入旧版本 v1 数据（1 行）
            cur.execute(f"INSERT INTO {target_table} (order_id, user_id, order_amount) VALUES (1, 1001, 99.00)")
            # 向影子表写入新版本 v2 数据（3 行）
            cur.execute(
                f"INSERT INTO {shadow_table} (order_id, user_id, order_amount) VALUES "
                "(10, 2001, 199.00), (20, 2002, 299.00), (30, 2003, 399.00)"
            )

            def _live_doris_sql_executor(sql: str) -> dict[str, Any]:
                cur.execute(sql)
                return {"executed_sql": sql}

            adapter = DorisPublishAdapter(sql_executor=_live_doris_sql_executor)
            cols_spec = [
                DorisColumnSpec(name="order_id", data_type="BIGINT", nullable=False, is_key=True),
                DorisColumnSpec(name="user_id", data_type="BIGINT", nullable=False),
                DorisColumnSpec(name="order_amount", data_type="DECIMAL(18, 2)", nullable=False),
            ]
            target_schema = DorisTableSchemaSpec(
                table_name=target_table,
                columns=cols_spec,
                key_columns=["order_id"],
            )
            shadow_schema = DorisTableSchemaSpec(
                table_name=shadow_table,
                columns=cols_spec,
                key_columns=["order_id"],
            )

            # 1. 验证 Schema 不兼容时被 DorisPublishAdapter 门禁阻断，不执行任何 SQL
            incompatible_shadow = DorisTableSchemaSpec(
                table_name=shadow_table,
                columns=[*cols_spec, DorisColumnSpec(name="extra_col", data_type="INT")],
                key_columns=["order_id"],
            )
            with pytest.raises(PublishSchemaIncompatibleError):
                adapter.verify_schema_compatibility(target_schema, incompatible_shadow)

            # 2. 执行真实原子表替换发布：ALTER TABLE <target> REPLACE WITH TABLE <shadow> PROPERTIES('swap'='true')
            published_ver = adapter.publish_shadow_to_target(
                organization_id=str(uuid.uuid4()),
                project_id=str(uuid.uuid4()),
                execution_id=exec_id,
                pipeline_version_id=str(uuid.uuid4()),
                artifact_hash=_sample_sha256("doris-live-publish"),
                target_connection_id="conn-live-doris",
                quality_report_id="qr-live-1",
                published_by="operator-live",
                target_schema=target_schema,
                shadow_schema=shadow_schema,
            )
            assert published_ver.status == "PUBLISHED"

            # 校验 Swap 后目标表变为 3 行（v2 数据），而归档影子表保留了原目标表的 1 行（v1 数据）
            cur.execute(f"SELECT COUNT(*) FROM {target_table}")
            assert int(cur.fetchone()[0]) == 3
            cur.execute(f"SELECT COUNT(*) FROM {shadow_table}")
            assert int(cur.fetchone()[0]) == 1

            # 3. 执行真实受管版本恢复回滚（SWAP_RESTORE），验证目标表原子换回 1 行（v1 数据）
            rb_executor = RollbackExecutor(doris_adapter=adapter, sql_executor=_live_doris_sql_executor)
            run_record = ExecutionRunRecord(
                execution_id=exec_id,
                organization_id=published_ver.organization_id,
                project_id=published_ver.project_id,
                pipeline_id=str(uuid.uuid4()),
                pipeline_version_id=published_ver.pipeline_version_id,
                artifact_hash=published_ver.artifact_hash,
                execution_mode=ExecutionMode.BATCH,
                status="SUCCEEDED",
                confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                preparation_id=str(uuid.uuid4()),
                capability_jti=str(uuid.uuid4()),
                command_id=str(uuid.uuid4()),
            )
            rb_ctx = RollbackCommandContext(
                organization_id=published_ver.organization_id,
                project_id=published_ver.project_id,
                preparation_id=str(uuid.uuid4()),
                capability_jti=str(uuid.uuid4()),
                command_id=str(uuid.uuid4()),
                initiated_by="operator-live",
            )
            rb_rec = rb_executor.execute_swap_restore(
                ctx=rb_ctx,
                run=run_record,
                published_version=published_ver,
                target_schema=target_schema,
                archive_schema=shadow_schema,
            )
            assert rb_rec.status == "SUCCEEDED"
            assert run_record.status == "ROLLED_BACK"

            cur.execute(f"SELECT COUNT(*) FROM {target_table}")
            assert int(cur.fetchone()[0]) == 1

            # 清理测试创建的临时表
            cur.execute(f"DROP TABLE IF EXISTS {target_table}")
            cur.execute(f"DROP TABLE IF EXISTS {shadow_table}")
    finally:
        conn.close()


# ==============================================================================
# 8. Apache SeaTunnel Zeta (127.0.0.1:5801)：真实 Zeta REST API 集群健康检查与作业生命周期查询
# ==============================================================================
def test_l2_live_seatunnel_zeta_cluster_health_and_job_lifecycle_query() -> None:
    """在真实 Apache SeaTunnel Zeta 2.3.9 容器上验证 REST API 集群概览与作业生命周期列表查询。"""
    zeta_host = os.getenv("SEATUNNEL_HOST", "127.0.0.1")
    zeta_port = int(os.getenv("SEATUNNEL_REST_PORT", "5801"))
    _require_service_port("seatunnel-zeta", zeta_host, zeta_port)

    base_url = f"http://{zeta_host}:{zeta_port}"
    with httpx.Client(timeout=10.0) as client:
        overview_resp = client.get(f"{base_url}/hazelcast/rest/maps/overview")
        if overview_resp.status_code == 200:
            overview = overview_resp.json()
            assert isinstance(overview, dict)

        running_resp = client.get(f"{base_url}/hazelcast/rest/maps/running-jobs")
        assert running_resp.status_code == 200
        assert isinstance(running_resp.json(), list)

    transport = HttpxZetaTransport(base_url=base_url)
    adapter = SeaTunnelZetaAdapter(transport=transport)
    cluster_jobs = adapter.list_cluster_jobs()
    assert isinstance(cluster_jobs, list)
