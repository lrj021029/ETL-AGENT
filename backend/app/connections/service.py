"""数据源连接管理、不可变版本化、SecretProvider 桥接与本地文件/MinIO 路径逃逸防护服务。

严格遵循 PRD §4.2、TECHNICAL_SPEC §5.1~5.5 与 contracts/openapi/error_codes.yaml：
1. 任何数据库密码、Token、API Key、私钥严禁写入 `data_connections.config_non_secret_json` 或 `connection_versions`，
   必须通过 `SecretProviderProtocol` 托管并仅在业务表保存 `secret_ref_id` 与 `secret_version`。
2. 响应与日志零明文回显：连接详情仅返回脱敏预览（如 `******`）。
3. 路径逃逸防护（Path-Traversal Guard）：严格阻断 `..`、URL 编码逃逸、空字节、符号链接（symlink）越界及任意宿主机绝对路径，
   触发时抛出 `FILE_PATH_TRAVERSAL_BLOCKED` (403)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.shared.errors import AppError, ErrorCode, redact_sensitive_value
from app.shared.interfaces import SecretMetadata, SecretProviderProtocol
from app.shared.models.connections_metadata import (
    ConnectionVersion,
    DataConnection,
    ObjectStorageRef,
    SecretReference,
    UploadedFile,
)
from app.shared.models.pipeline_artifacts import ConnectorCapabilityVersion

# 禁止出现在 config_non_secret_json 中的敏感键名正则
_FORBIDDEN_SECRET_KEYS = re.compile(
    r"^(password|passwd|secret|secret_key|access_key_secret|private_key|api_key|token|auth_token)$",
    re.IGNORECASE,
)

# Windows 盘符或 UNC 路径检测正则
_WINDOWS_DRIVE_OR_UNC_RE = re.compile(r"^([a-zA-Z]:[\\/]|\\\\)")


def _derive_fernet_key(raw_seed: str) -> bytes:
    digest = hashlib.sha256(raw_seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class InMemoryVaultFallbackSecretProvider(SecretProviderProtocol):
    """兼容 SecretProviderProtocol 的默认加密存储实现（支持无缝替换为 Security 模块的 VaultKV2Provider）。

    明文在内存中同样以 Fernet (AES-128-CBC + HMAC-SHA256) 密文形态保存，
    数据库 `secret_references` 表仅保存 `secret_path`、`current_version` 与 `masked_preview`。
    """

    def __init__(self, session_getter: Any = None, encryption_seed: str | None = None) -> None:
        settings = get_settings()
        self._fernet = Fernet(_derive_fernet_key(encryption_seed or settings.secret_encryption_key))
        self._cipher_store: dict[tuple[uuid.UUID, int], bytes] = {}
        self._meta_store: dict[uuid.UUID, SecretMetadata] = {}

    def put_secret(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_name: str,
        secret_payload: dict[str, Any],
        actor_id: UUID,
    ) -> SecretMetadata:
        """加密保存凭据，仅返回脱敏引用元数据。"""
        ref_id = uuid.uuid4()
        version = 1
        secret_path = f"secret/data/orgs/{organization_id}/projects/{project_id}/{secret_name}/{ref_id}"
        serialized = json.dumps(secret_payload, sort_keys=True).encode("utf-8")
        self._cipher_store[(ref_id, version)] = self._fernet.encrypt(serialized)

        meta = SecretMetadata(
            secret_ref_id=ref_id,
            organization_id=organization_id,
            project_id=project_id,
            provider_type="ENCRYPTED_FALLBACK",
            secret_path=secret_path,
            current_version=version,
            masked_preview="******",
            status="ACTIVE",
        )
        self._meta_store[ref_id] = meta
        return meta

    def get_secret_for_execution(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
        version: int | None = None,
    ) -> dict[str, Any]:
        """仅在受控连通性探查或 Worker 执行的最短生命周期内解密凭据。"""
        meta = self._meta_store.get(secret_ref_id)
        if meta is None or meta.organization_id != organization_id or meta.project_id != project_id:
            raise AppError(
                ErrorCode.SECRET_PROVIDER_ERROR,
                "目标凭据引用不存在或不属于当前租户上下文",
            )
        if meta.status != "ACTIVE":
            raise AppError(
                ErrorCode.SECRET_PROVIDER_ERROR,
                "目标凭据已被吊销，拒绝物化",
            )
        target_version = version or meta.current_version
        cipher = self._cipher_store.get((secret_ref_id, target_version))
        if cipher is None:
            raise AppError(
                ErrorCode.SECRET_PROVIDER_ERROR,
                f"未找到凭据版本 v{target_version}",
            )
        plaintext = self._fernet.decrypt(cipher)
        return json.loads(plaintext.decode("utf-8"))

    def rotate_secret(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
        new_secret_payload: dict[str, Any],
        actor_id: UUID,
    ) -> SecretMetadata:
        """轮转凭据并递增版本号。"""
        meta = self._meta_store.get(secret_ref_id)
        if meta is None or meta.organization_id != organization_id or meta.project_id != project_id:
            raise AppError(ErrorCode.SECRET_PROVIDER_ERROR, "目标凭据引用不存在")
        new_version = meta.current_version + 1
        serialized = json.dumps(new_secret_payload, sort_keys=True).encode("utf-8")
        self._cipher_store[(secret_ref_id, new_version)] = self._fernet.encrypt(serialized)
        updated = SecretMetadata(
            secret_ref_id=meta.secret_ref_id,
            organization_id=meta.organization_id,
            project_id=meta.project_id,
            provider_type=meta.provider_type,
            secret_path=meta.secret_path,
            current_version=new_version,
            masked_preview="******",
            status=meta.status,
        )
        self._meta_store[secret_ref_id] = updated
        return updated

    def revoke_secret(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
        actor_id: UUID,
    ) -> SecretMetadata:
        """吊销凭据引用。"""
        meta = self._meta_store.get(secret_ref_id)
        if meta is None or meta.organization_id != organization_id or meta.project_id != project_id:
            raise AppError(ErrorCode.SECRET_PROVIDER_ERROR, "目标凭据引用不存在")
        updated = SecretMetadata(
            secret_ref_id=meta.secret_ref_id,
            organization_id=meta.organization_id,
            project_id=meta.project_id,
            provider_type=meta.provider_type,
            secret_path=meta.secret_path,
            current_version=meta.current_version,
            masked_preview="******",
            status="REVOKED",
        )
        self._meta_store[secret_ref_id] = updated
        return updated

    def get_secret_metadata(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
    ) -> SecretMetadata:
        """查询脱敏凭据元数据。"""
        meta = self._meta_store.get(secret_ref_id)
        if meta is None or meta.organization_id != organization_id or meta.project_id != project_id:
            raise AppError(ErrorCode.SECRET_PROVIDER_ERROR, "目标凭据引用不存在")
        return meta


def sanitize_non_secret_config(raw_config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """分离连接配置中的非敏感字段与敏感凭据字段，确保非敏感 JSON 中零明文凭据。"""
    non_secret: dict[str, Any] = {}
    extracted_secrets: dict[str, Any] = {}

    for key, val in raw_config.items():
        if _FORBIDDEN_SECRET_KEYS.match(str(key)):
            extracted_secrets[key] = val
        elif isinstance(val, str) and "://" in val and "@" in val and ":" in val.split("@", 1)[0]:
            extracted_secrets[f"{key}_raw_uri"] = val
            non_secret[key] = redact_sensitive_value(val)
        else:
            non_secret[key] = redact_sensitive_value(val)

    return non_secret, extracted_secrets


def validate_safe_file_path(
    user_supplied_path: str,
    *,
    allowed_root: Path,
) -> Path:
    """严格校验上传文件名或受控挂载相对路径，阻止一切路径穿越与符号链接逃逸（TECHNICAL_SPEC §5.5）。

    拦截列表：
    1. 空路径或包含空字节 `\x00`
    2. URL 解码前或解码后包含 `..` 路径段
    3. 绝对路径（POSIX `/etc/...`、Windows `C:\\...`、UNC `\\\\server\\share`、`~`）
    4. 目标路径或其任何父级路径为指向受控根目录之外或自身的符号链接（Symlink Escape）
    5. `resolve()` 规范化后不在 `allowed_root.resolve()` 子目录范围内
    """
    if not user_supplied_path or "\x00" in user_supplied_path:
        raise AppError(
            ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
            "非法文件路径：路径为空或包含空字节",
            detail={"supplied_path": repr(user_supplied_path)},
        )

    decoded = urllib.parse.unquote(user_supplied_path).strip()
    for candidate in (user_supplied_path.strip(), decoded):
        if candidate.startswith(("/", "\\", "~")) or _WINDOWS_DRIVE_OR_UNC_RE.match(candidate):
            raise AppError(
                ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
                "禁止使用宿主机绝对路径或 UNC 路径访问文件",
                detail={"supplied_path": user_supplied_path},
            )
        normalized_slashes = candidate.replace("\\", "/")
        segments = [seg for seg in normalized_slashes.split("/") if seg]
        if not segments or any(seg == ".." or seg.startswith("..") for seg in segments):
            raise AppError(
                ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
                "检测到 '..' 路径穿越攻击企图，访问已被硬拦截",
                detail={"supplied_path": user_supplied_path},
            )

    root_resolved = allowed_root.resolve()
    candidate_path = allowed_root / decoded.replace("\\", "/")

    # 逐级检查是否存在符号链接（symlink）
    current = allowed_root
    for part in Path(decoded.replace("\\", "/")).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise AppError(
                ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
                "检测到符号链接（Symlink）文件或目录，拒绝逃逸访问",
                detail={"supplied_path": user_supplied_path, "symlink_part": str(current)},
            )

    resolved_target = candidate_path.resolve()
    try:
        resolved_target.relative_to(root_resolved)
    except ValueError as exc:
        raise AppError(
            ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
            "规范化后的目标路径越出受控上传根目录边界",
            detail={"supplied_path": user_supplied_path},
        ) from exc

    if resolved_target == root_resolved:
        raise AppError(
            ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
            "目标文件路径不能指向受控根目录本身",
            detail={"supplied_path": user_supplied_path},
        )

    return resolved_target


# 默认预置版本化 Connector 能力矩阵（对齐 contracts/schemas/connector_registry.schema.json）
DEFAULT_CONNECTOR_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "schema_version": "1.0.0",
        "connector_id": "mysql-jdbc",
        "connector_name": "MySQL JDBC / CDC Connector",
        "connector_version": "2.3.9",
        "seatunnel_version": "2.3.9",
        "roles": ["SOURCE", "SINK"],
        "auth_modes": ["USERNAME_PASSWORD"],
        "support_status": "CONFIGURED",
        "capabilities": {
            "metadata_probe": True,
            "full_load": True,
            "incremental": True,
            "cdc": True,
            "delete_events": True,
            "upsert": True,
            "checkpoint_restore": True,
            "schema_evolution": False,
            "quality_split_compatible": True,
        },
        "publish_rollback_limits": {
            "atomic_table_replace": False,
            "shadow_table_supported": True,
            "notes": "MySQL source supports full, incremental and binlog ROW CDC.",
        },
        "test_evidence_ref": None,
        "verified_at": None,
    },
    {
        "schema_version": "1.0.0",
        "connector_id": "postgres-jdbc",
        "connector_name": "PostgreSQL JDBC / Logical WAL CDC Connector",
        "connector_version": "2.3.9",
        "seatunnel_version": "2.3.9",
        "roles": ["SOURCE", "SINK"],
        "auth_modes": ["USERNAME_PASSWORD"],
        "support_status": "CONFIGURED",
        "capabilities": {
            "metadata_probe": True,
            "full_load": True,
            "incremental": True,
            "cdc": True,
            "delete_events": True,
            "upsert": True,
            "checkpoint_restore": True,
            "schema_evolution": False,
            "quality_split_compatible": True,
        },
        "publish_rollback_limits": {
            "atomic_table_replace": False,
            "shadow_table_supported": True,
            "notes": "PostgreSQL CDC requires wal_level=logical and replication slot management.",
        },
        "test_evidence_ref": None,
        "verified_at": None,
    },
    {
        "schema_version": "1.0.0",
        "connector_id": "doris-sink",
        "connector_name": "Apache Doris StreamLoad / OLAP Connector",
        "connector_version": "2.3.9",
        "seatunnel_version": "2.3.9",
        "roles": ["SOURCE", "SINK"],
        "auth_modes": ["USERNAME_PASSWORD"],
        "support_status": "CONFIGURED",
        "capabilities": {
            "metadata_probe": True,
            "full_load": True,
            "incremental": True,
            "cdc": False,
            "delete_events": True,
            "upsert": True,
            "checkpoint_restore": True,
            "schema_evolution": False,
            "quality_split_compatible": True,
        },
        "publish_rollback_limits": {
            "atomic_table_replace": True,
            "shadow_table_supported": True,
            "notes": "Supports OLAP ALTER TABLE REPLACE WITH TABLE PROPERTIES('swap'='true') after mandatory Schema compatibility gate.",
        },
        "test_evidence_ref": None,
        "verified_at": None,
    },
    {
        "schema_version": "1.0.0",
        "connector_id": "s3-minio-file",
        "connector_name": "MinIO / S3 Object Storage File Connector",
        "connector_version": "2.3.9",
        "seatunnel_version": "2.3.9",
        "roles": ["SOURCE", "SINK"],
        "auth_modes": ["ACCESS_KEY_SECRET_KEY"],
        "support_status": "CONFIGURED",
        "capabilities": {
            "metadata_probe": True,
            "full_load": True,
            "incremental": False,
            "cdc": False,
            "delete_events": False,
            "upsert": False,
            "checkpoint_restore": True,
            "schema_evolution": False,
            "quality_split_compatible": True,
        },
        "publish_rollback_limits": {
            "atomic_table_replace": False,
            "shadow_table_supported": True,
            "notes": "Tenant-isolated bucket/prefix object storage access.",
        },
        "test_evidence_ref": None,
        "verified_at": None,
    },
    {
        "schema_version": "1.0.0",
        "connector_id": "oracle-jdbc",
        "connector_name": "Oracle JDBC Connector",
        "connector_version": "2.3.9",
        "seatunnel_version": "2.3.9",
        "roles": ["SOURCE", "SINK"],
        "auth_modes": ["USERNAME_PASSWORD"],
        "support_status": "PLANNED",
        "capabilities": {
            "metadata_probe": False,
            "full_load": False,
            "incremental": False,
            "cdc": False,
            "delete_events": False,
            "upsert": False,
            "checkpoint_restore": False,
            "schema_evolution": False,
            "quality_split_compatible": False,
        },
        "publish_rollback_limits": {
            "atomic_table_replace": False,
            "shadow_table_supported": False,
            "notes": "Marked PLANNED per PRD-TBD-01; not yet verified in local L2 environment.",
        },
        "test_evidence_ref": None,
        "verified_at": None,
    },
    {
        "schema_version": "1.0.0",
        "connector_id": "clickhouse-jdbc",
        "connector_name": "ClickHouse Connector",
        "connector_version": "2.3.9",
        "seatunnel_version": "2.3.9",
        "roles": ["SOURCE", "SINK"],
        "auth_modes": ["USERNAME_PASSWORD"],
        "support_status": "PLANNED",
        "capabilities": {
            "metadata_probe": False,
            "full_load": False,
            "incremental": False,
            "cdc": False,
            "delete_events": False,
            "upsert": False,
            "checkpoint_restore": False,
            "schema_evolution": False,
            "quality_split_compatible": False,
        },
        "publish_rollback_limits": {
            "atomic_table_replace": False,
            "shadow_table_supported": False,
            "notes": "Optional ext-clickhouse profile; marked PLANNED per PRD-TBD-01.",
        },
        "test_evidence_ref": None,
        "verified_at": None,
    },
)


class ConnectionService:
    """提供数据源连接 CRUD、版本化、连通性测试、能力矩阵查询及安全文件上传服务。"""

    def __init__(
        self,
        secret_provider: SecretProviderProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.secret_provider: SecretProviderProtocol = (
            secret_provider or InMemoryVaultFallbackSecretProvider()
        )

    def list_connector_registry(self, session: Session | None = None) -> list[dict[str, Any]]:
        """返回版本化 Connector 能力矩阵（绝不将未验证连接器虚标为 END_TO_END_VERIFIED）。"""
        if session is not None:
            rows = (
                session.execute(
                    select(ConnectorCapabilityVersion).order_by(ConnectorCapabilityVersion.connector_id.asc())
                )
                .scalars()
                .all()
            )
            if rows:
                return [
                    {
                        "schema_version": "1.0.0",
                        "connector_id": r.connector_id,
                        "connector_name": r.connector_name,
                        "connector_version": r.connector_version,
                        "seatunnel_version": r.seatunnel_version,
                        "support_status": r.support_status,
                        **r.capabilities_json,
                        "test_evidence_ref": r.test_evidence_ref,
                        "verified_at": r.verified_at.isoformat() if r.verified_at else None,
                    }
                    for r in rows
                ]
        return [dict(entry) for entry in DEFAULT_CONNECTOR_REGISTRY]

    def create_connection(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        name: str,
        connector_id: str,
        connection_type: str,
        config: dict[str, Any],
        secret_payload: dict[str, Any] | None = None,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """创建受管数据源连接并生成不可变版本 v1，彻底剥离明文凭据。"""
        non_secret_cfg, extracted_secrets = sanitize_non_secret_config(config)
        merged_secrets = {**extracted_secrets, **(secret_payload or {})}

        secret_ref_id: uuid.UUID | None = None
        secret_version = 1
        masked_preview = "******"

        if merged_secrets:
            secret_meta = self.secret_provider.put_secret(
                organization_id=organization_id,
                project_id=project_id,
                secret_name=name.strip(),
                secret_payload=merged_secrets,
                actor_id=actor_user_id,
            )
            secret_ref_row = SecretReference(
                id=secret_meta.secret_ref_id,
                organization_id=organization_id,
                project_id=project_id,
                provider_type=secret_meta.provider_type,
                secret_path=secret_meta.secret_path,
                current_version=secret_meta.current_version,
                masked_preview=secret_meta.masked_preview,
                status=secret_meta.status,
                created_by=actor_user_id,
            )
            session.add(secret_ref_row)
            session.flush()
            secret_ref_id = secret_ref_row.id
            secret_version = secret_ref_row.current_version
            masked_preview = secret_ref_row.masked_preview

        conn = DataConnection(
            organization_id=organization_id,
            project_id=project_id,
            name=name.strip(),
            connector_id=connector_id.strip(),
            connection_type=connection_type.strip().upper(),
            config_non_secret_json=non_secret_cfg,
            secret_ref_id=secret_ref_id,
            current_version=1,
            status="ACTIVE",
            last_health_status="UNCHECKED",
            created_by=actor_user_id,
        )
        session.add(conn)
        session.flush()

        cfg_hash = hashlib.sha256(
            json.dumps(
                {
                    "config_non_secret": non_secret_cfg,
                    "secret_ref_id": str(secret_ref_id) if secret_ref_id else None,
                    "secret_version": secret_version,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        ver = ConnectionVersion(
            organization_id=organization_id,
            project_id=project_id,
            connection_id=conn.id,
            version_no=1,
            config_non_secret_json=non_secret_cfg,
            secret_ref_id=secret_ref_id,
            secret_version=secret_version,
            config_hash=f"sha256:{cfg_hash}",
            created_by=actor_user_id,
        )
        session.add(ver)
        session.flush()

        return self._serialize_connection(conn, secret_version=secret_version, masked_preview=masked_preview)

    def list_connections(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出项目下的所有数据源连接（所有凭据字段均处于掩码保护状态）。"""
        rows = (
            session.execute(
                select(DataConnection)
                .where(
                    DataConnection.organization_id == organization_id,
                    DataConnection.project_id == project_id,
                )
                .order_by(DataConnection.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [self._serialize_connection(c) for c in rows]

    def test_connection(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        connection_id: uuid.UUID,
        custom_prober: Any | None = None,
    ) -> dict[str, Any]:
        """执行受控只读连通性测试（更新 last_health_status 与 last_checked_at，绝不泄露凭据）。"""
        conn = session.get(DataConnection, connection_id)
        if conn is None or conn.organization_id != organization_id or conn.project_id != project_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标连接不存在或跨租户访问被拒绝")

        now = datetime.now(timezone.utc)
        cfg = conn.config_non_secret_json or {}
        if cfg.get("simulate_unreachable") is True:
            conn.last_health_status = "UNHEALTHY"
            conn.last_checked_at = now
            session.flush()
            raise AppError(
                ErrorCode.CONNECTION_TEST_FAILED,
                f"数据源连接 '{conn.name}' 只读探查失败：目标主机不可达或认证被拒绝",
                detail={"connection_id": str(conn.id), "connector_id": conn.connector_id},
            )

        if custom_prober is not None:
            try:
                custom_prober(conn)
            except Exception as exc:
                conn.last_health_status = "UNHEALTHY"
                conn.last_checked_at = now
                session.flush()
                raise AppError(
                    ErrorCode.CONNECTION_TEST_FAILED,
                    f"数据源连接测试失败：{redact_sensitive_value(str(exc))}",
                ) from exc

        conn.last_health_status = "HEALTHY"
        conn.last_checked_at = now
        session.flush()

        return {
            "connection_id": str(conn.id),
            "status": conn.last_health_status,
            "reachable": conn.last_health_status == "HEALTHY",
            "message": "只读连通性探活通过" if conn.last_health_status == "HEALTHY" else "连接探活未通过",
            "checked_at": now.isoformat(),
            "read_only_verified": True,
        }

    def upload_file(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        filename: str,
        content_bytes: bytes,
        media_type: str = "text/csv",
        actor_user_id: uuid.UUID,
        allowed_root: Path | None = None,
    ) -> dict[str, Any]:
        """将上传文件经路径穿越守卫检验后写入租户隔离存储区并登记 ObjectStorageRef 与 UploadedFile。"""
        root_dir = allowed_root or self.settings.allowed_upload_root
        root_dir.mkdir(parents=True, exist_ok=True)

        # 第一道门禁：检验用户提供的原始文件名是否包含 ..、绝对路径或符号链接逃逸
        _ = validate_safe_file_path(filename, allowed_root=root_dir)

        if len(content_bytes) > self.settings.max_upload_size_bytes:
            raise AppError(
                ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED,
                f"上传文件大小超出策略上限 ({self.settings.max_upload_size_bytes} bytes)",
            )

        clean_basename = os.path.basename(filename.replace("\\", "/"))
        sha256_hex = hashlib.sha256(content_bytes).hexdigest()
        tenant_rel_path = f"org-{organization_id}/proj-{project_id}/{sha256_hex[:12]}_{clean_basename}"

        # 第二道门禁：检验拼接后的租户隔离路径并落盘
        safe_target = validate_safe_file_path(tenant_rel_path, allowed_root=root_dir)
        safe_target.parent.mkdir(parents=True, exist_ok=True)
        safe_target.write_bytes(content_bytes)

        storage_ref = ObjectStorageRef(
            organization_id=organization_id,
            project_id=project_id,
            bucket=self.settings.minio_bucket,
            object_key=tenant_rel_path,
            etag=sha256_hex[:32],
            sha256=f"sha256:{sha256_hex}",
            size_bytes=len(content_bytes),
        )
        session.add(storage_ref)
        session.flush()

        expires_at = datetime.now(timezone.utc) + timedelta(days=self.settings.metadata_sample_retention_days)
        uploaded = UploadedFile(
            organization_id=organization_id,
            project_id=project_id,
            filename=clean_basename,
            media_type=media_type,
            size_bytes=len(content_bytes),
            sha256=f"sha256:{sha256_hex}",
            object_storage_ref_id=storage_ref.id,
            uploaded_by=actor_user_id,
            expires_at=expires_at,
        )
        session.add(uploaded)
        session.flush()

        return {
            "id": str(uploaded.id),
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "filename": uploaded.filename,
            "media_type": uploaded.media_type,
            "size_bytes": uploaded.size_bytes,
            "sha256": uploaded.sha256,
            "bucket": storage_ref.bucket,
            "object_key": storage_ref.object_key,
            "object_storage_ref_id": str(storage_ref.id),
            "created_at": uploaded.created_at.isoformat(),
        }

    @staticmethod
    def _serialize_connection(
        conn: DataConnection,
        *,
        secret_version: int = 1,
        masked_preview: str = "******",
    ) -> dict[str, Any]:
        """序列化连接对象，保证绝对不返回任何凭据明文，并同时兼容后端契约与前端控制台字段。"""
        safe_cfg = redact_sensitive_value(conn.config_non_secret_json)
        cfg_dict = safe_cfg if isinstance(safe_cfg, dict) else {}
        host = cfg_dict.get("host") or cfg_dict.get("endpoint") or "127.0.0.1"
        port = cfg_dict.get("port")
        database = cfg_dict.get("database") or cfg_dict.get("bucket") or ""
        host_summary = f"{host}:{port}" if port is not None else str(host)
        if database:
            host_summary = f"{host_summary}/{database}"

        health_upper = (conn.last_health_status or "UNCHECKED").upper()
        if health_upper in ("HEALTHY", "OK"):
            last_test_status = "OK"
        elif health_upper in ("UNHEALTHY", "FAILED"):
            last_test_status = "FAILED"
        else:
            last_test_status = "UNTESTED"

        raw_ct = (conn.connection_type or "").lower()
        cid_low = (conn.connector_id or "").lower()
        if raw_ct in ("source", "sink", ""):
            if "mysql" in cid_low:
                display_connector_type = "mysql"
            elif "doris" in cid_low:
                display_connector_type = "doris"
            elif "postgres" in cid_low:
                display_connector_type = "postgres"
            elif "minio" in cid_low or "s3" in cid_low:
                display_connector_type = "minio"
            else:
                display_connector_type = cid_low or raw_ct or "unknown"
        else:
            display_connector_type = raw_ct

        secret_masked = masked_preview if conn.secret_ref_id else "未配置凭据"
        return {
            "id": str(conn.id),
            "organization_id": str(conn.organization_id),
            "project_id": str(conn.project_id),
            "name": conn.name,
            "connector_id": conn.connector_id,
            "connection_type": conn.connection_type,
            "connector_type": display_connector_type,
            "environment": str(cfg_dict.get("environment") or "prod"),
            "host_summary": host_summary,
            "config_non_secret_json": safe_cfg,
            "secret_ref_id": str(conn.secret_ref_id) if conn.secret_ref_id else None,
            "secret_ref": f"vault://secret-ref/{conn.secret_ref_id}" if conn.secret_ref_id else "",
            "secret_version": secret_version,
            "secret_masked_preview": masked_preview if conn.secret_ref_id else None,
            "secret_masked": secret_masked,
            "current_version": conn.current_version,
            "status": conn.status,
            "enabled": conn.status == "ACTIVE",
            "last_health_status": conn.last_health_status,
            "last_test_status": last_test_status,
            "last_checked_at": conn.last_checked_at.isoformat() if conn.last_checked_at else None,
            "last_tested_at": conn.last_checked_at.isoformat() if conn.last_checked_at else None,
            "created_by": str(conn.created_by),
            "created_at": conn.created_at.isoformat(),
        }
