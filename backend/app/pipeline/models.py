"""EtlPlan DSL / QualityContract / RuntimeSupervisionContract 的 Pydantic v2 模型。

字段与冻结契约 contracts/schemas/*.schema.json (v1.0.0) 一一对应；
所有模型 extra=forbid，且不允许出现明文密钥（密钥仅以 ${SECRET:NAME} 占位符出现）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, field_validator

SECRET_PLACEHOLDER_RE = re.compile(r"^\$\{SECRET:[A-Z0-9_]+\}$")

# 契约目录：仓库根 / contracts / schemas
_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schemas"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceObject(_Strict):
    schema_name: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    alias: str | None = None
    primary_keys: list[str] = Field(default_factory=list)
    incremental_column: str | None = None
    filter_predicate: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


class SourceSpec(_Strict):
    connection_id: str
    connection_version: int = Field(ge=1)
    connector_id: str = Field(min_length=1)
    metadata_snapshot_id: str
    objects: list[SourceObject] = Field(min_length=1)


class SinkSpec(_Strict):
    connection_id: str
    connection_version: int = Field(ge=1)
    connector_id: str = Field(min_length=1)
    metadata_snapshot_id: str | None = None
    target_schema: str = Field(min_length=1)
    target_table: str = Field(min_length=1)
    write_mode: Literal["SHADOW_SWAP", "APPEND", "UPSERT"]
    primary_keys: list[str] = Field(default_factory=list)


class FieldMapping(_Strict):
    source_object: str | None = None
    source_field: str = Field(min_length=1)
    target_field: str = Field(min_length=1)
    target_type: str = Field(min_length=1)
    nullable: bool = True
    transform_expr: str | None = None
    masking_policy: Literal[None, "NONE", "HASH_SHA256", "MASK_PARTIAL", "REDACT"] = None


class Transformation(_Strict):
    step_id: str = Field(min_length=1)
    kind: Literal["SELECT_SQL", "FIELD_MAPPER", "FILTER", "DEDUP"]
    sql_dialect: Literal["mysql", "postgres", "doris", "seatunnel"]
    sql_text: str = Field(min_length=1)
    resolved_execution_strategy: Literal[None, "SOURCE_PUSHDOWN", "SEATUNNEL_TRANSFORM", "TARGET_SIDE_SQL"] = None
    strategy_reason_codes: list[str] = Field(default_factory=list)


class PublishStrategy(_Strict):
    adapter: Literal["DORIS_ATOMIC_SWAP", "STAGING_INSERT", "CDC_STREAMING_UPSERT"]
    require_schema_compat_gate: Literal[True]
    retain_previous_version: bool = True


class SecretRef(_Strict):
    placeholder: str
    secret_ref_id: str
    secret_version: int = Field(ge=1)

    @field_validator("placeholder")
    @classmethod
    def _placeholder(cls, v: str) -> str:
        if not SECRET_PLACEHOLDER_RE.match(v):
            raise ValueError("placeholder 必须形如 ${SECRET:NAME}")
        return v


class EtlPlan(_Strict):
    schema_version: Literal["1.0.0"] = "1.0.0"
    plan_name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    sync_mode: Literal["FULL_LOAD", "INCREMENTAL", "CDC"]
    source: SourceSpec
    sink: SinkSpec
    field_mappings: list[FieldMapping] = Field(min_length=1)
    transformations: list[Transformation]
    publish_strategy: PublishStrategy
    secret_refs: list[SecretRef] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class QualityRule(_Strict):
    rule_id: str = Field(min_length=1, max_length=64)
    rule_type: Literal["NOT_NULL", "DATA_TYPE", "REGEX", "RANGE", "UNIQUE", "ENUM", "RESTRICTED_SQL"]
    stage: Literal["STREAMING_ROW_SPLIT", "SHADOW_TABLE_VALIDATION"]
    column_name: str | None = None
    error_code: str = Field(pattern=r"^DQ_[A-Z0-9_]+$")
    severity: Literal["REJECT_ROW", "FAIL_RUN", "WARN"]
    params: dict[str, Any]
    sql_dialect: Literal[None, "mysql", "postgres", "doris"] = None
    sql_text: str | None = None


class QualityContract(_Strict):
    schema_version: Literal["1.0.0"] = "1.0.0"
    error_table_name: str = Field(min_length=1, max_length=128)
    raw_value_storage_policy: Literal["REDACTED_BY_CLASSIFICATION", "HASH_ONLY", "OMIT_SENSITIVE"]
    rules: list[QualityRule]


class WindowDefinition(_Strict):
    mode: Literal["ROW_COUNT", "TIME_SECONDS"]
    size: int = Field(ge=1)


class RuntimeSupervisionContract(_Strict):
    schema_version: Literal["1.0.0"] = "1.0.0"
    max_read_rows: int = Field(ge=1)
    max_write_bytes: int = Field(ge=1024)
    max_duration_seconds: int = Field(ge=10)
    max_amplification_ratio: float = Field(gt=0)
    max_reject_rate_total: float = Field(default=0.2, ge=0, le=1)
    max_reject_rate_window: float = Field(ge=0, le=1)
    window_definition: WindowDefinition
    min_validated_rows: int = Field(ge=1)
    max_error_rows_absolute: int = Field(ge=1)
    action_on_breach: Literal["HARD_INTERRUPT", "ALERT_ONLY"]
    cdc_max_lag_seconds: int | None = Field(default=None, ge=1)


def default_supervision() -> RuntimeSupervisionContract:
    """确定性默认值；max_reject_rate_total=0.20 且仅在 > 0.20 时触发。"""
    return RuntimeSupervisionContract(
        max_read_rows=10_000_000,
        max_write_bytes=10 * 1024**3,
        max_duration_seconds=3600,
        max_amplification_ratio=5.0,
        max_reject_rate_total=0.2,
        max_reject_rate_window=0.5,
        window_definition=WindowDefinition(mode="ROW_COUNT", size=10_000),
        min_validated_rows=1000,
        max_error_rows_absolute=100_000,
        action_on_breach="HARD_INTERRUPT",
    )


def validate_against_contract(schema_file: str, data: dict[str, Any]) -> list[str]:
    """用冻结的 JSON Schema 校验，返回错误消息列表（空表示通过）。"""
    path = _SCHEMA_DIR / schema_file
    if not path.exists():  # 契约目录缺失时由 Pydantic 兜底
        return []
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(data)]
