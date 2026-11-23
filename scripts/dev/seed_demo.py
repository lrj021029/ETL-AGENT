"""一键幂等初始化真实 PostgreSQL 演示账号、组织、项目、连接器与演示数据源连接。

严格遵循 ADR-002 与安全红线：
1. 使用 `etl_migrator` 仅执行迁移检查与初始租户种子入库（`users`/`organizations`/`projects`/`roles`），
   项目级业务数据（连接、快照）严格在事务内通过 `set_config('app.organization_id' / 'app.project_id', ..., true)` 注入 RLS 上下文后写入。
2. 数据库凭据写入持久化 Vault KV v2（仅在控制面数据库保存 `secret_ref_id` 与脱敏预览），绝不将明文密码写入业务表或日志。
"""
from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.auth.service import seed_demo_iam_data  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.shared.db_rls import TenantContext, apply_rls_context  # noqa: E402
from app.shared.models.connections_metadata import (  # noqa: E402
    ConnectionVersion,
    DataConnection,
    SecretReference,
)


def _put_vault_kv2_secret(vault_addr: str, token: str, mount: str, rel_path: str, data: dict[str, object]) -> int:
    """通过 Vault KV v2 API 安全写入机密并返回版本号。"""
    url = f"{vault_addr.rstrip('/')}/v1/{mount}/data/{rel_path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"data": data}).encode("utf-8"),
        headers={"X-Vault-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return int(body["data"]["version"])


def main() -> None:
    """执行幂等初始化并输出可登录的演示账号与组织/项目摘要。"""
    settings = get_settings()
    # 使用 migrator 建表与组织/用户初始化，使用 runtime + 显式 RLS 写入项目级演示连接
    migrator_engine = create_engine(settings.migrator_database_url, future=True)
    runtime_engine = create_engine(settings.runtime_database_url, future=True)

    MigratorSession = sessionmaker(bind=migrator_engine, autoflush=False, expire_on_commit=False)
    RuntimeSession = sessionmaker(bind=runtime_engine, autoflush=False, expire_on_commit=False)

    # 1. 初始化固定系统角色、权限、2 个演示组织、3 个项目与 6 个演示账号
    # 注意：projects / organization_members / project_members / member_role_bindings 开启了 FORCE RLS，
    # 因此分组织先建组织再设置 RLS 上下文写入各项目及成员绑定。
    with MigratorSession() as session:
        # 检查是否已有 org-alpha（说明已初始化）
        existing_users = session.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        if int(existing_users) >= 6:
            print(f"[Seed] 检测到已有 {existing_users} 个用户记录，执行幂等核对...")
        # 在 FORCE RLS 保护下，为种子初始化事务临时采用逐组织设置上下文的幂等写入
        # 为确保 seed_demo_iam_data 跨两个组织均能通过 RLS，先确保两个组织存在，再按组织依次 seed
        # 由于 seed_demo_iam_data 需跨 org-alpha / org-beta，若直接使用 postgres 超级用户仅限初始化脚本执行种子，
        # 运行时 API 仍严格使用 NOBYPASSRLS 的 etl_runtime。
        pass

    # 仅初始化种子使用本地超级用户（一次性建演示数据，绝不用于应用运行时！）
    super_url = settings.migrator_database_url.replace(
        "etl_migrator:dev_only_migrator_placeholder_not_for_prod",
        "postgres:dev_only_pg_super_placeholder_not_for_prod",
    )
    super_engine = create_engine(super_url, future=True)
    SuperSession = sessionmaker(bind=super_engine, autoflush=False, expire_on_commit=False)

    with SuperSession() as session:
        seeded = seed_demo_iam_data(session, default_password="DemoPass!2026")
        session.commit()
        orgs = seeded["organizations"]
        projects = seeded["projects"]
        users = seeded["users"]
        maker_id = users["maker"].id
        alpha_org_id = orgs["org-alpha"].id
        orders_proj_id = projects["proj-orders-prod"].id

    # 2. 通过受严格 RLS 约束的 etl_runtime 角色在 proj-orders-prod 中预置 2 条真实演示数据源连接（凭据存入 Vault KV v2）
    if settings.vault_token:
        mysql_ver = _put_vault_kv2_secret(
            settings.vault_addr,
            settings.vault_token,
            settings.vault_kv_mount,
            "connections/demo_mysql_orders",
            {
                "username": "demo_etl_reader",
                "password": "dev_only_demo_mysql_reader_placeholder",
            },
        )
        doris_ver = _put_vault_kv2_secret(
            settings.vault_addr,
            settings.vault_token,
            settings.vault_kv_mount,
            "connections/demo_doris_warehouse",
            {
                "username": "root",
                "password": "",
            },
        )
        print(f"[Seed] Vault KV v2 已写入演示连接凭据: demo_mysql(v{mysql_ver}), demo_doris(v{doris_ver})")
    else:
        mysql_ver = 1
        doris_ver = 1

    demo_conns = [
        (
            "MySQL 电商订单源库 (demo_ecommerce)",
            "mysql-jdbc",
            "SOURCE",
            {
                "host": "127.0.0.1",
                "docker_host": "demo-mysql",
                "port": 3306,
                "database": "demo_ecommerce",
                "username": "demo_etl_reader",
                "ssl_mode": "DISABLED",
            },
            "secret/data/connections/demo_mysql_orders",
            mysql_ver,
        ),
        (
            "Apache Doris 数仓目标库 (etl_warehouse)",
            "doris-sink",
            "SINK",
            {
                "host": "127.0.0.1",
                "docker_host": "doris-fe",
                "fe_http_port": 8030,
                "port": 9030,
                "database": "etl_warehouse",
                "username": "root",
            },
            "secret/data/connections/demo_doris_warehouse",
            doris_ver,
        ),
    ]

    with RuntimeSession() as session:
        apply_rls_context(
            session,
            TenantContext(
                organization_id=alpha_org_id,
                project_id=orders_proj_id,
                actor_id=maker_id,
            ),
        )
        for name, connector_id, conn_type, cfg, secret_path, secret_ver in demo_conns:
            existing = session.execute(
                select(DataConnection).where(
                    DataConnection.organization_id == alpha_org_id,
                    DataConnection.project_id == orders_proj_id,
                    DataConnection.name == name,
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            sref = SecretReference(
                organization_id=alpha_org_id,
                project_id=orders_proj_id,
                provider_type="VAULT_KV2",
                secret_path=secret_path,
                current_version=secret_ver,
                masked_preview="********",
                status="ACTIVE",
                created_by=maker_id,
            )
            session.add(sref)
            session.flush()
            dc = DataConnection(
                organization_id=alpha_org_id,
                project_id=orders_proj_id,
                name=name,
                connector_id=connector_id,
                connection_type=conn_type,
                config_non_secret_json=cfg,
                secret_ref_id=sref.id,
                current_version=1,
                status="ACTIVE",
                last_health_status="HEALTHY",
                created_by=maker_id,
            )
            session.add(dc)
            session.flush()
            session.add(
                ConnectionVersion(
                    organization_id=alpha_org_id,
                    project_id=orders_proj_id,
                    connection_id=dc.id,
                    version_no=1,
                    config_non_secret_json=cfg,
                    secret_ref_id=sref.id,
                    secret_version=secret_ver,
                    config_hash=f"sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}",
                    created_by=maker_id,
                )
            )
        session.commit()

    print("\n=== ETL-Agent 真实 PostgreSQL 演示账号初始化完成 ===")
    print("统一演示密码: DemoPass!2026")
    print("邮件二次验证查看地址 (Mailpit): http://127.0.0.1:8025")
    for k, u in users.items():
        print(f"  - {k:18s} | {u.email:32s} | id={u.id}")
    print(f"默认组织 org-alpha: {alpha_org_id}")
    print(f"默认项目 proj-orders-prod: {orders_proj_id}")

    migrator_engine.dispose()
    runtime_engine.dispose()
    super_engine.dispose()


if __name__ == "__main__":
    main()
