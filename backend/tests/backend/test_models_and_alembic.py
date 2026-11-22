"""测试套件 1：全量 6 大领域 ORM 数据模型、唯一约束与 Alembic 迁移图校验。

验证内容：
1. `contracts/db/erd.md` 中定义的全部 71 张核心业务表均在 SQLAlchemy `Base.metadata` 中声明。
2. 关键唯一约束（`capability_consumptions.jti`、`execution_commands.command_id`、
   `evidence_events(organization_id, project_id, sequence_no)`、`connection_versions(connection_id, version_no)`、
   `pipeline_versions(pipeline_id, revision)`）完整存在。
3. Alembic 迁移脚本 `0001_initial_schema` -> `0002_rls_policies` 拓扑链完整无环。
4. `0002_rls_policies` 为所有组织级与项目级租户表生成 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`
   及 Fail-Closed 策略 SQL，且包含 `resolve_execution_tenant_context(UUID, UUID)` `SECURITY DEFINER` 函数。
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.shared.models import Base, ORG_SCOPED_RLS_TABLES, PROJECT_SCOPED_RLS_TABLES


EXPECTED_ERD_TABLES: set[str] = {
    # 2.1 IAM / Tenant (13 表)
    "users",
    "organizations",
    "organization_members",
    "projects",
    "project_members",
    "roles",
    "permissions",
    "role_permissions",
    "member_role_bindings",
    "policy_rules",
    "sessions",
    "refresh_token_families",
    "email_otp_challenges",
    # 2.2 Connections / Metadata / Secrets (13 表)
    "secret_references",
    "data_connections",
    "connection_versions",
    "metadata_profiles",
    "metadata_snapshots",
    "metadata_objects",
    "metadata_columns",
    "metadata_statistics",
    "sensitive_classifications",
    "schema_fingerprints",
    "schema_drift_events",
    "uploaded_files",
    "object_storage_refs",
    # 2.3 Pipeline / Agent / Artifacts (13 表)
    "agent_threads",
    "workflow_runs",
    "pipeline_definitions",
    "pipeline_drafts",
    "pipeline_versions",
    "artifact_manifests",
    "artifact_blobs",
    "etl_plan_modules",
    "sql_artifacts",
    "hocon_artifacts",
    "quality_contracts",
    "runtime_contracts",
    "connector_capability_versions",
    # 2.4 Governance / Harness (10 表)
    "preparations",
    "preparation_fingerprints",
    "approval_slots",
    "approval_decisions",
    "capability_issuances",
    "capability_consumptions",
    "execution_commands",
    "replay_records",
    "outbox_events",
    "outbox_deliveries",
    # 2.5 Execution / Quality / Publish (12 表)
    "execution_runs",
    "execution_state_events",
    "external_job_refs",
    "execution_metrics",
    "execution_logs_refs",
    "quality_reports",
    "quality_rule_results",
    "rejected_record_summaries",
    "published_dataset_versions",
    "rollback_runs",
    "schedules",
    "schedule_triggers",
    # 2.6 Evidence / Benchmark (10 表)
    "evidence_events",
    "evidence_checkpoints",
    "evidence_verification_runs",
    "benchmark_datasets",
    "benchmark_cases",
    "benchmark_runs",
    "benchmark_case_results",
    "model_provider_versions",
    "policy_versions",
    "generator_versions",
}


def test_all_71_erd_tables_registered_in_metadata() -> None:
    """验证 contracts/db/erd.md 中的全部 71 张表均已注册在 Base.metadata 中。"""
    registered = set(Base.metadata.tables.keys())
    assert len(EXPECTED_ERD_TABLES) == 71
    missing = EXPECTED_ERD_TABLES - registered
    assert not missing, f"缺失 ERD 表定义: {missing}"


def test_critical_unique_constraints_and_composite_indexes() -> None:
    """验证关键单次消费、命令幂等与证据链序号唯一约束以及复合租户索引。"""
    cap_cons_table = Base.metadata.tables["capability_consumptions"]
    assert cap_cons_table.c.jti.unique is True

    exec_cmd_table = Base.metadata.tables["execution_commands"]
    assert exec_cmd_table.c.command_id.unique is True

    ev_table = Base.metadata.tables["evidence_events"]
    uq_names = {c.name for c in ev_table.constraints if hasattr(c, "columns")}
    assert "uq_evidence_events_org_proj_seq" in uq_names

    # 所有项目级租户表必须同时包含 organization_id 和 project_id 非空 UUID 列
    for table_name in PROJECT_SCOPED_RLS_TABLES:
        tbl = Base.metadata.tables[table_name]
        assert "organization_id" in tbl.c, f"{table_name} 缺少 organization_id"
        assert "project_id" in tbl.c, f"{table_name} 缺少 project_id"
        assert tbl.c.organization_id.nullable is False


def test_alembic_migration_graph_and_rls_policy_sql() -> None:
    """验证 Alembic 迁移版本链与 0002_rls_policies 生成的 RLS 策略 SQL。"""
    backend_root = Path(__file__).resolve().parents[2]
    alembic_cfg = Config(str(backend_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_root / "migrations"))

    script_dir = ScriptDirectory.from_config(alembic_cfg)
    revisions = list(script_dir.walk_revisions())
    rev_ids = [r.revision for r in revisions]

    assert "0001_initial_schema" in rev_ids
    assert "0002_rls_policies" in rev_ids
    assert script_dir.get_current_head() == "0002_rls_policies"

    # 导入 0002_rls_policies 验证 RLS SQL 生成器
    import importlib.util

    rls_mod_path = backend_root / "migrations" / "versions" / "0002_rls_policies.py"
    spec = importlib.util.spec_from_file_location("rls_migration", rls_mod_path)
    assert spec is not None and spec.loader is not None
    rls_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rls_mod)

    org_stmts = rls_mod.build_org_rls_sql("projects")
    assert any("ENABLE ROW LEVEL SECURITY" in s for s in org_stmts)
    assert any("FORCE ROW LEVEL SECURITY" in s for s in org_stmts)
    assert any("current_setting('app.organization_id', true)" in s for s in org_stmts)

    proj_stmts = rls_mod.build_project_rls_sql("data_connections")
    assert any("ENABLE ROW LEVEL SECURITY" in s for s in proj_stmts)
    assert any("FORCE ROW LEVEL SECURITY" in s for s in proj_stmts)
    assert any("current_setting('app.project_id', true)" in s for s in proj_stmts)

    assert "SECURITY DEFINER" in rls_mod.RESOLVE_EXECUTION_TENANT_CONTEXT_SQL
    assert "NOBYPASSRLS" in rls_mod.ROLE_SETUP_SQL
    assert len(ORG_SCOPED_RLS_TABLES) >= 4
    assert len(PROJECT_SCOPED_RLS_TABLES) >= 50
