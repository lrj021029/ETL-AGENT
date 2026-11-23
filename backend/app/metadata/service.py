"""受管只读元数据探查、不可变 MetadataSnapshot 持久化、SchemaDriftEvent 差异引擎与敏感列分级服务。

严格遵循 PRD §4.2、TECHNICAL_SPEC §5.2 与 contracts/db/erd.md §2.2：
1. 每次元数据刷新/探查必须插入全新的 `MetadataSnapshot`，严禁原地覆盖历史 Snapshot。
2. 每次生成新 Snapshot 时计算确定性 `SchemaFingerprint` (SHA256)；若同一连接存在上一版本 Snapshot，
   自动计算表/列/类型/可空性/主键差异并生成 `SchemaDriftEvent`。
3. 自动检测敏感字段（PII/凭据/财务）并支持人工覆盖标记（`update_manual_classification`）。
4. 采样数据在落库前必须按敏感分类执行脱敏（`masked_sample_json`），并绑定可配置的样本过期时间 `sample_expires_at`。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.shared.errors import AppError, ErrorCode, redact_sensitive_value
from app.shared.models.connections_metadata import (
    DataConnection,
    MetadataColumn,
    MetadataObject,
    MetadataProfile,
    MetadataSnapshot,
    MetadataStatistic,
    SchemaDriftEvent,
    SchemaFingerprint,
    SecretReference,
    SensitiveClassification,
)

# 敏感字段自动检测规则集：(正则, classification_level, pii_category)
_SENSITIVE_COLUMN_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(password|passwd|secret|private_key|api_key|auth_token|access_token)", re.I),
        "RESTRICTED_PII",
        "CREDENTIAL",
    ),
    (
        re.compile(r"(id_card|ssn|national_id|passport_no|tax_id)", re.I),
        "RESTRICTED_PII",
        "GOVERNMENT_ID",
    ),
    (
        re.compile(r"(bank_card|credit_card|card_no|iban|cvv)", re.I),
        "RESTRICTED_PII",
        "FINANCIAL_ACCOUNT",
    ),
    (
        re.compile(r"(phone|mobile|tel_no|cellphone)", re.I),
        "CONFIDENTIAL",
        "PHONE",
    ),
    (
        re.compile(r"(email|e_mail|mail_address)", re.I),
        "CONFIDENTIAL",
        "EMAIL",
    ),
    (
        re.compile(r"(home_address|billing_address|shipping_address|street_address)", re.I),
        "CONFIDENTIAL",
        "ADDRESS",
    ),
    (
        re.compile(r"(salary|income|bonus_amount)", re.I),
        "CONFIDENTIAL",
        "COMPENSATION",
    ),
)


def auto_detect_sensitive_column(column_name: str, comment: str | None = None) -> tuple[str, str | None]:
    """根据列名与注释自动识别敏感分级与 PII 类别。"""
    target_text = f"{column_name} {comment or ''}"
    for pattern, level, category in _SENSITIVE_COLUMN_RULES:
        if pattern.search(target_text):
            return level, category
    return "INTERNAL", None


def _mask_single_cell(val: Any, pii_category: str | None, level: str) -> Any:
    """根据敏感分级与 PII 类别对单个样本值进行脱敏。"""
    if val is None:
        return None
    if level in ("PUBLIC", "INTERNAL"):
        return redact_sensitive_value(val)

    s = str(val)
    if pii_category == "CREDENTIAL":
        return "***REDACTED_CREDENTIAL***"
    if pii_category == "EMAIL" and "@" in s:
        local, domain = s.split("@", 1)
        prefix = local[:1] if local else "*"
        return f"{prefix}***@{domain}"
    if pii_category == "PHONE" and len(s) >= 7:
        return f"{s[:3]}****{s[-2:]}"
    if pii_category in ("GOVERNMENT_ID", "FINANCIAL_ACCOUNT") and len(s) >= 6:
        return f"{s[:2]}******{s[-2:]}"
    if len(s) > 4:
        return f"{s[:2]}***{s[-1:]}"
    return "***"


def mask_sample_rows(
    sample_rows: list[dict[str, Any]],
    classifications: dict[str, tuple[str, str | None]],
    *,
    max_rows: int = 20,
) -> list[dict[str, Any]]:
    """对采集到的样本行按列级敏感分类执行脱敏裁剪。"""
    masked: list[dict[str, Any]] = []
    for row in sample_rows[:max_rows]:
        clean_row: dict[str, Any] = {}
        for col_name, raw_val in row.items():
            level, category = classifications.get(col_name, auto_detect_sensitive_column(col_name))
            clean_row[col_name] = _mask_single_cell(raw_val, category, level)
        masked.append(clean_row)
    return masked


def compute_schema_fingerprint(objects_spec: list[dict[str, Any]]) -> str:
    """对探查出的表/列/类型/可空性/主键结构计算确定性 SHA256 指纹。"""
    canonical_objects = []
    for obj in sorted(objects_spec, key=lambda o: (o.get("schema_name", ""), o.get("object_name", ""))):
        cols = sorted(
            [
                {
                    "column_name": c["column_name"],
                    "data_type": str(c["data_type"]).upper(),
                    "nullable": bool(c.get("nullable", True)),
                }
                for c in obj.get("columns", [])
            ],
            key=lambda c: c["column_name"],
        )
        canonical_objects.append(
            {
                "schema_name": obj.get("schema_name", "public"),
                "object_name": obj["object_name"],
                "object_type": obj.get("object_type", "TABLE"),
                "primary_key": sorted(str(k) for k in obj.get("primary_key", [])),
                "columns": cols,
            }
        )
    serialized = json.dumps(canonical_objects, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_schema_drift(
    prev_objects: list[dict[str, Any]],
    curr_objects: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """对比两个快照的对象结构，返回 `(severity, diff_json)`。

    严重度分级：
    - `NONE`：完全无变化
    - `LOW_ADDITIVE`：仅新增可空列或新增表
    - `MEDIUM_COMPATIBLE`：列长度/兼容扩展或非空转可空
    - `HIGH_BREAKING`：删除表、删除列、修改列数据类型、主键变更或新增非空无默认值列
    """
    prev_map = {f"{o.get('schema_name', 'public')}.{o['object_name']}": o for o in prev_objects}
    curr_map = {f"{o.get('schema_name', 'public')}.{o['object_name']}": o for o in curr_objects}

    added_tables = sorted(set(curr_map.keys()) - set(prev_map.keys()))
    removed_tables = sorted(set(prev_map.keys()) - set(curr_map.keys()))

    added_columns: list[dict[str, Any]] = []
    removed_columns: list[dict[str, Any]] = []
    type_changes: list[dict[str, Any]] = []
    nullability_changes: list[dict[str, Any]] = []
    primary_key_changes: list[dict[str, Any]] = []

    for table_key in sorted(set(prev_map.keys()) & set(curr_map.keys())):
        p_obj = prev_map[table_key]
        c_obj = curr_map[table_key]

        p_pk = sorted(str(k) for k in p_obj.get("primary_key", []))
        c_pk = sorted(str(k) for k in c_obj.get("primary_key", []))
        if p_pk != c_pk:
            primary_key_changes.append({"table": table_key, "from_pk": p_pk, "to_pk": c_pk})

        p_cols = {c["column_name"]: c for c in p_obj.get("columns", [])}
        c_cols = {c["column_name"]: c for c in c_obj.get("columns", [])}

        for col_name in sorted(set(c_cols.keys()) - set(p_cols.keys())):
            col = c_cols[col_name]
            added_columns.append(
                {
                    "table": table_key,
                    "column_name": col_name,
                    "data_type": col["data_type"],
                    "nullable": bool(col.get("nullable", True)),
                }
            )

        for col_name in sorted(set(p_cols.keys()) - set(c_cols.keys())):
            col = p_cols[col_name]
            removed_columns.append(
                {
                    "table": table_key,
                    "column_name": col_name,
                    "data_type": col["data_type"],
                }
            )

        for col_name in sorted(set(p_cols.keys()) & set(c_cols.keys())):
            p_col = p_cols[col_name]
            c_col = c_cols[col_name]
            p_type = str(p_col["data_type"]).upper()
            c_type = str(c_col["data_type"]).upper()
            if p_type != c_type:
                type_changes.append(
                    {
                        "table": table_key,
                        "column_name": col_name,
                        "from_type": p_type,
                        "to_type": c_type,
                    }
                )
            p_null = bool(p_col.get("nullable", True))
            c_null = bool(c_col.get("nullable", True))
            if p_null != c_null:
                nullability_changes.append(
                    {
                        "table": table_key,
                        "column_name": col_name,
                        "from_nullable": p_null,
                        "to_nullable": c_null,
                    }
                )

    has_breaking = bool(
        removed_tables
        or removed_columns
        or type_changes
        or primary_key_changes
        or any(not c["nullable"] for c in added_columns)
        or any(c["from_nullable"] and not c["to_nullable"] for c in nullability_changes)
    )
    has_medium = bool(nullability_changes)
    has_additive = bool(added_tables or added_columns)

    if has_breaking:
        severity = "HIGH_BREAKING"
    elif has_medium:
        severity = "MEDIUM_COMPATIBLE"
    elif has_additive:
        severity = "LOW_ADDITIVE"
    else:
        severity = "NONE"

    diff_json = {
        "added_tables": added_tables,
        "removed_tables": removed_tables,
        "added_columns": added_columns,
        "removed_columns": removed_columns,
        "type_changes": type_changes,
        "nullability_changes": nullability_changes,
        "primary_key_changes": primary_key_changes,
    }
    return severity, diff_json


@runtime_checkable
class MetadataProberProtocol(Protocol):
    """受管只读元数据探查器协议接口（支持 MySQL / PostgreSQL / Doris / 确定性测试探查）。"""

    def probe_connection(
        self,
        connection: DataConnection,
        *,
        secret_payload: dict[str, Any] | None = None,
        sample_max_rows: int = 20,
    ) -> list[dict[str, Any]]:
        """执行只读探查并返回表/列/主键/索引/统计与原始采样行列表。"""
        ...


class SqlAlchemyReadOnlyMetadataProber(MetadataProberProtocol):
    """基于 SQLAlchemy Inspector（支持 MySQL PyMySQL、PostgreSQL Psycopg、Doris MySQL 协议及 SQLite）的真实只读探查器。"""

    def probe_connection(
        self,
        connection: DataConnection,
        *,
        secret_payload: dict[str, Any] | None = None,
        sample_max_rows: int = 20,
    ) -> list[dict[str, Any]]:
        """连接目标数据库执行只读 Schema 探查；若连接配置提供 `inline_schema_objects` 则用于离线确定性探查。"""
        cfg = connection.config_non_secret_json or {}
        if "inline_schema_objects" in cfg and isinstance(cfg["inline_schema_objects"], list):
            return list(cfg["inline_schema_objects"])

        db_url = self._build_database_url(connection, secret_payload or {})
        conn_hint = f"{connection.connection_type} {connection.connector_id}".upper()
        use_backticks = ("MYSQL" in conn_hint) or ("DORIS" in conn_hint)
        engine = create_engine(db_url, pool_pre_ping=True, future=True)
        discovered_objects: list[dict[str, Any]] = []
        try:
            with engine.connect() as conn:
                inspector = inspect(conn)
                schema_name = (
                    cfg.get("schema")
                    or (cfg.get("database") if use_backticks else None)
                    or inspector.default_schema_name
                    or "public"
                )
                inspect_schema = None if schema_name == "main" else schema_name
                for table_name in inspector.get_table_names(schema=inspect_schema):
                    raw_cols = inspector.get_columns(table_name, schema=inspect_schema)
                    try:
                        pk_constraint = inspector.get_pk_constraint(table_name, schema=inspect_schema)
                    except Exception:
                        pk_constraint = {}
                    try:
                        indexes = inspector.get_indexes(table_name, schema=inspect_schema)
                    except Exception:
                        indexes = []

                    columns_spec = [
                        {
                            "column_name": col["name"],
                            "data_type": str(col["type"]),
                            "nullable": bool(col.get("nullable", True)),
                            "ordinal_position": idx + 1,
                            "default_expr": str(col.get("default")) if col.get("default") is not None else None,
                            "comment": col.get("comment"),
                        }
                        for idx, col in enumerate(raw_cols)
                    ]

                    sample_rows: list[dict[str, Any]] = []
                    row_count_approx = 0
                    try:
                        if use_backticks:
                            qualified = f"`{schema_name}`.`{table_name}`"
                        else:
                            qualified = (
                                f'"{table_name}"'
                                if schema_name in ("main", "public")
                                else f'"{schema_name}"."{table_name}"'
                            )
                        count_res = conn.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar()
                        row_count_approx = int(count_res or 0)
                        rows = conn.execute(
                            text(f"SELECT * FROM {qualified} LIMIT :lim"),
                            {"lim": int(sample_max_rows)},
                        ).mappings().all()
                        sample_rows = [
                            {k: (str(v) if not isinstance(v, (int, float, bool, str, type(None))) else v) for k, v in dict(r).items()}
                            for r in rows
                        ]
                    except Exception:
                        pass

                    discovered_objects.append(
                        {
                            "schema_name": schema_name,
                            "object_name": table_name,
                            "object_type": "TABLE",
                            "row_count_approx": row_count_approx,
                            "bytes_approx": row_count_approx * 128,
                            "primary_key": pk_constraint.get("constrained_columns", []),
                            "indexes": [
                                {"name": ix.get("name"), "columns": ix.get("column_names", [])}
                                for ix in indexes
                            ],
                            "columns": columns_spec,
                            "sample_rows": sample_rows,
                        }
                    )
        except Exception as exc:
            raise AppError(
                ErrorCode.METADATA_PROBE_FAILED,
                f"元数据只读探查失败：{redact_sensitive_value(str(exc))}",
                detail={"connection_id": str(connection.id)},
            ) from exc
        finally:
            engine.dispose()

        return discovered_objects

    @staticmethod
    def _build_database_url(connection: DataConnection, secret_payload: dict[str, Any]) -> str:
        """从非敏感配置与物化凭据组装 SQLAlchemy 只读连接 URL。"""
        cfg = connection.config_non_secret_json or {}
        if "sqlalchemy_url" in cfg:
            return str(cfg["sqlalchemy_url"])

        host = str(cfg.get("host", "127.0.0.1"))
        port = cfg.get("port", 3306)
        # 宿主机本地运行时将 Docker Compose 内部服务名映射回 127.0.0.1
        if host in ("demo-mysql", "doris-fe", "demo-postgres", "postgres"):
            import socket

            try:
                socket.gethostbyname(host)
            except OSError:
                host = "127.0.0.1"
        database = cfg.get("database", "etl_demo")
        username = secret_payload.get("username") or cfg.get("username", "readonly")
        password = secret_payload.get("password", "")

        conn_hint = f"{connection.connection_type} {connection.connector_id}".upper()
        if ("MYSQL" in conn_hint) or ("DORIS" in conn_hint):
            return f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}"
        if "POSTGRES" in conn_hint:
            return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{database}"
        raise AppError(
            ErrorCode.METADATA_PROBE_FAILED,
            f"不支持直接 SQL 元数据探查的连接类型：{connection.connection_type} ({connection.connector_id})",
        )


class MetadataService:
    """不可变元数据快照采集、敏感列自动/手动标记及 Schema Drift 对比引擎服务。"""

    def __init__(
        self,
        prober: MetadataProberProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.prober: MetadataProberProtocol = prober or SqlAlchemyReadOnlyMetadataProber()

    def probe_and_create_snapshot(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        override_objects: list[dict[str, Any]] | None = None,
        connector_version: str = "2.3.9",
    ) -> dict[str, Any]:
        """执行只读元数据探查并创建全新的不可变 `MetadataSnapshot`。

        若当前连接已有历史 Snapshot，自动对比上一版本并记录 `SchemaDriftEvent`。
        """
        conn = session.get(DataConnection, connection_id)
        if conn is None or conn.organization_id != organization_id or conn.project_id != project_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标连接不存在或跨租户访问被拒绝")

        profile = session.execute(
            select(MetadataProfile).where(
                MetadataProfile.organization_id == organization_id,
                MetadataProfile.project_id == project_id,
                MetadataProfile.connection_id == connection_id,
            )
        ).scalar_one_or_none()
        if profile is None:
            profile = MetadataProfile(
                organization_id=organization_id,
                project_id=project_id,
                connection_id=connection_id,
                refresh_cron=None,
                sample_policy_json={
                    "max_rows": self.settings.metadata_sample_max_rows,
                    "retention_days": self.settings.metadata_sample_retention_days,
                },
            )
            session.add(profile)
            session.flush()

        prev_snapshot = (
            session.execute(
                select(MetadataSnapshot)
                .where(
                    MetadataSnapshot.organization_id == organization_id,
                    MetadataSnapshot.project_id == project_id,
                    MetadataSnapshot.connection_id == connection_id,
                )
                .order_by(MetadataSnapshot.captured_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        prev_objects_spec = (
            self._load_snapshot_objects_spec(session, prev_snapshot.id)
            if prev_snapshot is not None
            else []
        )

        secret_payload = self._resolve_connection_secret_payload(session, conn)
        discovered = (
            override_objects
            if override_objects is not None
            else self.prober.probe_connection(
                conn,
                secret_payload=secret_payload,
                sample_max_rows=self.settings.metadata_sample_max_rows,
            )
        )
        fingerprint_sha256 = compute_schema_fingerprint(discovered)

        now = datetime.now(timezone.utc)
        snapshot = MetadataSnapshot(
            organization_id=organization_id,
            project_id=project_id,
            profile_id=profile.id,
            connection_id=connection_id,
            connection_version=conn.current_version,
            connector_version=connector_version,
            schema_fingerprint=fingerprint_sha256,
            classification_rule_version=self.settings.metadata_classification_rule_version,
            stats_source="READ_ONLY_PROBE",
            captured_at=now,
            captured_by=actor_user_id,
        )
        session.add(snapshot)
        session.flush()

        fp_row = SchemaFingerprint(
            organization_id=organization_id,
            project_id=project_id,
            connection_id=connection_id,
            snapshot_id=snapshot.id,
            fingerprint_sha256=fingerprint_sha256,
            computed_at=now,
        )
        session.add(fp_row)

        sample_expires_at = now + timedelta(days=self.settings.metadata_sample_retention_days)
        serialized_objects: list[dict[str, Any]] = []

        for obj_spec in discovered:
            meta_obj = MetadataObject(
                organization_id=organization_id,
                project_id=project_id,
                snapshot_id=snapshot.id,
                schema_name=obj_spec.get("schema_name", "public"),
                object_name=obj_spec["object_name"],
                object_type=obj_spec.get("object_type", "TABLE"),
                row_count_approx=int(obj_spec.get("row_count_approx", 0)),
                bytes_approx=int(obj_spec.get("bytes_approx", 0)),
                primary_key_json=list(obj_spec.get("primary_key", [])),
                indexes_json=list(obj_spec.get("indexes", [])),
            )
            session.add(meta_obj)
            session.flush()

            col_classifications: dict[str, tuple[str, str | None]] = {}
            serialized_cols: list[dict[str, Any]] = []

            for idx, col_spec in enumerate(obj_spec.get("columns", [])):
                col_name = col_spec["column_name"]
                meta_col = MetadataColumn(
                    organization_id=organization_id,
                    project_id=project_id,
                    object_id=meta_obj.id,
                    column_name=col_name,
                    data_type=str(col_spec["data_type"]),
                    nullable=bool(col_spec.get("nullable", True)),
                    ordinal_position=int(col_spec.get("ordinal_position", idx + 1)),
                    default_expr=col_spec.get("default_expr"),
                    comment=col_spec.get("comment"),
                )
                session.add(meta_col)

                level, pii_cat = auto_detect_sensitive_column(col_name, col_spec.get("comment"))
                col_classifications[col_name] = (level, pii_cat)

                sens = SensitiveClassification(
                    organization_id=organization_id,
                    project_id=project_id,
                    snapshot_id=snapshot.id,
                    object_id=meta_obj.id,
                    column_name=col_name,
                    classification_level=level,
                    pii_category=pii_cat,
                    source_mode="AUTO_DETECT",
                    marked_by=actor_user_id,
                    marked_at=now,
                )
                session.add(sens)

                serialized_cols.append(
                    {
                        "column_name": col_name,
                        "data_type": meta_col.data_type,
                        "nullable": meta_col.nullable,
                        "classification_level": level,
                        "pii_category": pii_cat,
                    }
                )

            masked_samples = mask_sample_rows(
                list(obj_spec.get("sample_rows", [])),
                col_classifications,
                max_rows=self.settings.metadata_sample_max_rows,
            )
            stat = MetadataStatistic(
                organization_id=organization_id,
                project_id=project_id,
                snapshot_id=snapshot.id,
                object_id=meta_obj.id,
                stats_json={
                    "row_count_approx": meta_obj.row_count_approx,
                    "bytes_approx": meta_obj.bytes_approx,
                    "sample_count": len(masked_samples),
                },
                masked_sample_json=masked_samples,
                sample_expires_at=sample_expires_at,
            )
            session.add(stat)

            serialized_objects.append(
                {
                    "object_id": str(meta_obj.id),
                    "schema_name": meta_obj.schema_name,
                    "object_name": meta_obj.object_name,
                    "object_type": meta_obj.object_type,
                    "primary_key": meta_obj.primary_key_json,
                    "columns": serialized_cols,
                    "masked_sample_rows": masked_samples,
                }
            )

        drift_summary: dict[str, Any] | None = None
        if prev_snapshot is not None:
            severity, diff_json = compute_schema_drift(prev_objects_spec, discovered)
            drift_event = SchemaDriftEvent(
                organization_id=organization_id,
                project_id=project_id,
                connection_id=connection_id,
                from_snapshot_id=prev_snapshot.id,
                to_snapshot_id=snapshot.id,
                severity=severity,
                diff_json=diff_json,
                detected_at=now,
            )
            session.add(drift_event)
            session.flush()
            drift_summary = {
                "drift_event_id": str(drift_event.id),
                "from_snapshot_id": str(prev_snapshot.id),
                "to_snapshot_id": str(snapshot.id),
                "severity": severity,
                "diff": diff_json,
            }

        session.flush()
        frontend_tables = self._build_frontend_tables(session, snapshot.id)
        return {
            "id": str(snapshot.id),
            "snapshot_id": str(snapshot.id),
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "connection_id": str(connection_id),
            "connection_version": snapshot.connection_version,
            "connector_version": snapshot.connector_version,
            "schema_fingerprint": snapshot.schema_fingerprint,
            "classification_rule_version": snapshot.classification_rule_version,
            "captured_at": snapshot.captured_at.isoformat(),
            "objects": serialized_objects,
            "tables": frontend_tables,
            "drift_event": drift_summary,
        }

    def list_snapshots(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        connection_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """按采集时间倒序列出指定连接的所有不可变 MetadataSnapshot（附带表列与脱敏样本供前端展示）。"""
        rows = (
            session.execute(
                select(MetadataSnapshot)
                .where(
                    MetadataSnapshot.organization_id == organization_id,
                    MetadataSnapshot.project_id == project_id,
                    MetadataSnapshot.connection_id == connection_id,
                )
                .order_by(MetadataSnapshot.captured_at.desc())
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(s.id),
                "snapshot_id": str(s.id),
                "connection_id": str(s.connection_id),
                "connection_version": s.connection_version,
                "connector_version": s.connector_version,
                "schema_fingerprint": s.schema_fingerprint,
                "classification_rule_version": s.classification_rule_version,
                "stats_source": s.stats_source,
                "captured_at": s.captured_at.isoformat(),
                "captured_by": str(s.captured_by),
                "tables": self._build_frontend_tables(session, s.id),
            }
            for s in rows
        ]

    def list_drift_events(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        connection_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出指定连接的 Schema Drift 历史记录（同时输出结构化 diff_json 与前端扁平 diffs 列表）。"""
        rows = (
            session.execute(
                select(SchemaDriftEvent)
                .where(
                    SchemaDriftEvent.organization_id == organization_id,
                    SchemaDriftEvent.project_id == project_id,
                    SchemaDriftEvent.connection_id == connection_id,
                )
                .order_by(SchemaDriftEvent.detected_at.desc())
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(d.id),
                "connection_id": str(d.connection_id),
                "from_snapshot_id": str(d.from_snapshot_id),
                "to_snapshot_id": str(d.to_snapshot_id),
                "severity": d.severity,
                "diff_json": d.diff_json,
                "diffs": self._flatten_drift_diffs(d.diff_json or {}),
                "detected_at": d.detected_at.isoformat(),
            }
            for d in rows
        ]

    def update_manual_classification(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        object_id: uuid.UUID | None,
        column_name: str,
        classification_level: str,
        pii_category: str | None,
        actor_user_id: uuid.UUID,
        table_name: str | None = None,
    ) -> dict[str, Any]:
        """人工标注或覆盖快照内指定字段的敏感分级（source_mode 置为 MANUAL_MARK）。"""
        tag_alias_map = {
            "NONE": "PUBLIC",
            "PII": "RESTRICTED_PII",
            "PCI": "RESTRICTED_PII",
            "RESTRICTED": "RESTRICTED_PII",
            "CONFIDENTIAL": "CONFIDENTIAL",
            "PUBLIC": "PUBLIC",
            "INTERNAL": "INTERNAL",
            "RESTRICTED_PII": "RESTRICTED_PII",
        }
        valid_levels = {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED_PII"}
        raw_upper = classification_level.strip().upper()
        clean_level = tag_alias_map.get(raw_upper, raw_upper)
        if clean_level not in valid_levels:
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                f"无效的敏感分级 '{clean_level}'，允许值为 {sorted(valid_levels)}",
            )

        resolved_object_id = object_id
        if resolved_object_id is None and table_name:
            obj_row = session.execute(
                select(MetadataObject).where(
                    MetadataObject.organization_id == organization_id,
                    MetadataObject.project_id == project_id,
                    MetadataObject.snapshot_id == snapshot_id,
                    MetadataObject.object_name == table_name,
                )
            ).scalar_one_or_none()
            if obj_row is None:
                raise AppError(ErrorCode.TENANT_ACCESS_DENIED, f"快照内未找到表 '{table_name}'")
            resolved_object_id = obj_row.id

        if resolved_object_id is None:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "必须提供 object_id 或 table_name")

        existing = session.execute(
            select(SensitiveClassification).where(
                SensitiveClassification.organization_id == organization_id,
                SensitiveClassification.project_id == project_id,
                SensitiveClassification.snapshot_id == snapshot_id,
                SensitiveClassification.object_id == resolved_object_id,
                SensitiveClassification.column_name == column_name,
            )
        ).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.classification_level = clean_level
            existing.pii_category = pii_category
            existing.source_mode = "MANUAL_MARK"
            existing.marked_by = actor_user_id
            existing.marked_at = now
            record = existing
        else:
            record = SensitiveClassification(
                organization_id=organization_id,
                project_id=project_id,
                snapshot_id=snapshot_id,
                object_id=resolved_object_id,
                column_name=column_name,
                classification_level=clean_level,
                pii_category=pii_category,
                source_mode="MANUAL_MARK",
                marked_by=actor_user_id,
                marked_at=now,
            )
            session.add(record)

        session.flush()
        snap = session.get(MetadataSnapshot, snapshot_id)
        return {
            "id": str(snapshot_id) if table_name else str(record.id),
            "classification_id": str(record.id),
            "snapshot_id": str(record.snapshot_id),
            "connection_id": str(snap.connection_id) if snap else "",
            "schema_fingerprint": snap.schema_fingerprint if snap else "",
            "captured_at": snap.captured_at.isoformat() if snap else now.isoformat(),
            "object_id": str(record.object_id),
            "column_name": record.column_name,
            "classification_level": record.classification_level,
            "pii_category": record.pii_category,
            "source_mode": record.source_mode,
            "marked_by": str(record.marked_by),
            "marked_at": record.marked_at.isoformat(),
            "tables": self._build_frontend_tables(session, snapshot_id),
        }

    def _resolve_connection_secret_payload(
        self, session: Session, conn: DataConnection
    ) -> dict[str, Any] | None:
        """在最短执行生命周期内按需从持久化 Vault KV v2 读取连接凭据（绝不落盘或打印明文）。"""
        if not conn.secret_ref_id:
            return None
        ref = session.get(SecretReference, conn.secret_ref_id)
        if ref is None or ref.status != "ACTIVE":
            return None
        if ref.provider_type == "VAULT_KV2" and self.settings.vault_token:
            import urllib.request as _req

            clean_path = ref.secret_path.lstrip("/")
            if not clean_path.startswith("v1/"):
                clean_path = f"v1/{clean_path}"
            url = f"{self.settings.vault_addr.rstrip('/')}/{clean_path}"
            req = _req.Request(
                url,
                headers={"X-Vault-Token": self.settings.vault_token},
                method="GET",
            )
            try:
                with _req.urlopen(req, timeout=5) as resp:  # noqa: S310
                    parsed = json.loads(resp.read().decode("utf-8"))
                    data = dict(parsed.get("data", {}).get("data", {}))
                    return data or None
            except Exception:
                return None
        return None

    @staticmethod
    def _build_frontend_tables(session: Session, snapshot_id: uuid.UUID) -> list[dict[str, Any]]:
        """组装前端 MetadataSnapshot 视图所需的表、列、敏感标记及脱敏样本列表。"""
        level_to_tag = {
            "PUBLIC": "NONE",
            "INTERNAL": "NONE",
            "CONFIDENTIAL": "CONFIDENTIAL",
            "RESTRICTED_PII": "PII",
        }
        objs = (
            session.execute(select(MetadataObject).where(MetadataObject.snapshot_id == snapshot_id))
            .scalars()
            .all()
        )
        tables: list[dict[str, Any]] = []
        for obj in objs:
            cols = (
                session.execute(
                    select(MetadataColumn)
                    .where(MetadataColumn.object_id == obj.id)
                    .order_by(MetadataColumn.ordinal_position.asc())
                )
                .scalars()
                .all()
            )
            sens_rows = (
                session.execute(
                    select(SensitiveClassification).where(
                        SensitiveClassification.snapshot_id == snapshot_id,
                        SensitiveClassification.object_id == obj.id,
                    )
                )
                .scalars()
                .all()
            )
            sens_map = {s.column_name: s.classification_level for s in sens_rows}
            stat = session.execute(
                select(MetadataStatistic).where(
                    MetadataStatistic.snapshot_id == snapshot_id,
                    MetadataStatistic.object_id == obj.id,
                )
            ).scalar_one_or_none()
            sample_rows = list(stat.masked_sample_json or []) if stat else []
            pk_set = {str(k) for k in (obj.primary_key_json or [])}

            tables.append(
                {
                    "object_id": str(obj.id),
                    "schema_name": obj.schema_name,
                    "table_name": obj.object_name,
                    "approx_rows": obj.row_count_approx,
                    "columns": [
                        {
                            "name": c.column_name,
                            "data_type": c.data_type,
                            "nullable": c.nullable,
                            "is_primary_key": c.column_name in pk_set,
                            "sensitivity_tag": level_to_tag.get(
                                sens_map.get(c.column_name, "PUBLIC"), "NONE"
                            ),
                            "masked_samples": [
                                str(row[c.column_name])
                                for row in sample_rows[:3]
                                if isinstance(row, dict) and c.column_name in row
                            ],
                        }
                        for c in cols
                    ],
                }
            )
        return tables

    @staticmethod
    def _flatten_drift_diffs(diff_json: dict[str, Any]) -> list[dict[str, Any]]:
        """将结构化 diff_json 扁平化为前端可直接渲染的 diffs 条目列表。"""
        items: list[dict[str, Any]] = []
        for col in diff_json.get("added_columns", []):
            items.append(
                {
                    "table": col.get("table", ""),
                    "column": col.get("column_name", ""),
                    "change_type": "ADDED",
                    "before": None,
                    "after": col.get("data_type"),
                }
            )
        for col in diff_json.get("removed_columns", []):
            items.append(
                {
                    "table": col.get("table", ""),
                    "column": col.get("column_name", ""),
                    "change_type": "REMOVED",
                    "before": col.get("data_type"),
                    "after": None,
                }
            )
        for col in diff_json.get("type_changes", []):
            items.append(
                {
                    "table": col.get("table", ""),
                    "column": col.get("column_name", ""),
                    "change_type": "TYPE_CHANGED",
                    "before": col.get("from_type"),
                    "after": col.get("to_type"),
                }
            )
        for col in diff_json.get("nullability_changes", []):
            items.append(
                {
                    "table": col.get("table", ""),
                    "column": col.get("column_name", ""),
                    "change_type": "NULLABILITY_CHANGED",
                    "before": str(col.get("from_nullable")),
                    "after": str(col.get("to_nullable")),
                }
            )
        return items

    @staticmethod
    def _load_snapshot_objects_spec(session: Session, snapshot_id: uuid.UUID) -> list[dict[str, Any]]:
        """从数据库加载指定 Snapshot 的对象与列结构，用于与新 Snapshot 进行 Drift 对比。"""
        objs = (
            session.execute(select(MetadataObject).where(MetadataObject.snapshot_id == snapshot_id))
            .scalars()
            .all()
        )
        result: list[dict[str, Any]] = []
        for obj in objs:
            cols = (
                session.execute(
                    select(MetadataColumn)
                    .where(MetadataColumn.object_id == obj.id)
                    .order_by(MetadataColumn.ordinal_position.asc())
                )
                .scalars()
                .all()
            )
            result.append(
                {
                    "schema_name": obj.schema_name,
                    "object_name": obj.object_name,
                    "object_type": obj.object_type,
                    "primary_key": obj.primary_key_json,
                    "columns": [
                        {
                            "column_name": c.column_name,
                            "data_type": c.data_type,
                            "nullable": c.nullable,
                        }
                        for c in cols
                    ],
                }
            )
        return result
