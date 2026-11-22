"""两阶段质量契约（QualityContract）领域模型与错误定义。

对齐 `contracts/schemas/quality_contract.schema.json`、`contracts/openapi/error_codes.yaml` 与 `contracts/db/erd.md` §2.5。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any


class RawValueStoragePolicy(str, Enum):
    """错误表原始载荷的脱敏与保存策略枚举。"""

    REDACTED_BY_CLASSIFICATION = "REDACTED_BY_CLASSIFICATION"
    HASH_ONLY = "HASH_ONLY"
    OMIT_SENSITIVE = "OMIT_SENSITIVE"


class QualityRuleType(str, Enum):
    """质量契约支持的七类规则枚举。"""

    NOT_NULL = "NOT_NULL"
    DATA_TYPE = "DATA_TYPE"
    REGEX = "REGEX"
    RANGE = "RANGE"
    UNIQUE = "UNIQUE"
    ENUM = "ENUM"
    RESTRICTED_SQL = "RESTRICTED_SQL"


class QualityStage(str, Enum):
    """两阶段质量执行阶段枚举（ADR-004）。"""

    STREAMING_ROW_SPLIT = "STREAMING_ROW_SPLIT"
    SHADOW_TABLE_VALIDATION = "SHADOW_TABLE_VALIDATION"


class QualitySeverity(str, Enum):
    """质量规则触发时的处置级别。"""

    REJECT_ROW = "REJECT_ROW"
    FAIL_RUN = "FAIL_RUN"
    WARN = "WARN"


# 标准数据面质量错误码映射（对应 contracts/openapi/error_codes.yaml）
DEFAULT_RULE_ERROR_CODES: dict[QualityRuleType, str] = {
    QualityRuleType.NOT_NULL: "DQ_NOT_NULL_VIOLATION",
    QualityRuleType.DATA_TYPE: "DQ_DATA_TYPE_VIOLATION",
    QualityRuleType.REGEX: "DQ_REGEX_VIOLATION",
    QualityRuleType.RANGE: "DQ_RANGE_VIOLATION",
    QualityRuleType.UNIQUE: "DQ_UNIQUE_VIOLATION",
    QualityRuleType.ENUM: "DQ_ENUM_VIOLATION",
    QualityRuleType.RESTRICTED_SQL: "DQ_RESTRICTED_SQL_VIOLATION",
}

_ERROR_CODE_REGEX = re.compile(r"^DQ_[A-Z0-9_]+$")


class SqlAstPolicyViolationError(Exception):
    """当 Stage 2 质量 SQL 违反只读 SELECT AST 安全沙箱策略时抛出。"""

    error_code: str = "SQL_AST_POLICY_VIOLATION"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class QualityRuleModel:
    """单条质量规则定义。"""

    rule_id: str
    rule_type: QualityRuleType
    stage: QualityStage
    error_code: str
    severity: QualitySeverity
    params: dict[str, Any] = field(default_factory=dict)
    column_name: str | None = None
    sql_dialect: str | None = None
    sql_text: str | None = None

    def __post_init__(self) -> None:
        """校验规则字段与阶段绑定关系。"""
        if not self.rule_id or len(self.rule_id) > 64:
            raise ValueError(f"rule_id 长度必须在 1..64 之间: {self.rule_id}")
        if not _ERROR_CODE_REGEX.match(self.error_code):
            raise ValueError(f"error_code 必须匹配 ^DQ_[A-Z0-9_]+$: {self.error_code}")
        if self.sql_dialect is not None and self.sql_dialect not in ("mysql", "postgres", "doris"):
            raise ValueError(f"不支持的 sql_dialect: {self.sql_dialect}")
        # UNIQUE 与 RESTRICTED_SQL 必须在 Stage 2 (SHADOW_TABLE_VALIDATION) 执行 per ADR-004
        if self.rule_type in (QualityRuleType.UNIQUE, QualityRuleType.RESTRICTED_SQL):
            if self.stage != QualityStage.SHADOW_TABLE_VALIDATION:
                raise ValueError(
                    f"规则 {self.rule_id} ({self.rule_type.value}) 必须在 SHADOW_TABLE_VALIDATION 阶段执行"
                )

    def to_dict(self) -> dict[str, Any]:
        """转换为符合 `quality_contract.schema.json` 的字典。"""
        return {
            "rule_id": self.rule_id,
            "rule_type": self.rule_type.value,
            "stage": self.stage.value,
            "column_name": self.column_name,
            "error_code": self.error_code,
            "severity": self.severity.value,
            "params": dict(self.params),
            "sql_dialect": self.sql_dialect,
            "sql_text": self.sql_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityRuleModel:
        """从字典构造质量规则。"""
        return cls(
            rule_id=str(data["rule_id"]),
            rule_type=QualityRuleType(data["rule_type"]),
            stage=QualityStage(data["stage"]),
            column_name=data.get("column_name"),
            error_code=str(data["error_code"]),
            severity=QualitySeverity(data["severity"]),
            params=dict(data.get("params") or {}),
            sql_dialect=data.get("sql_dialect"),
            sql_text=data.get("sql_text"),
        )


@dataclass(frozen=True)
class QualityContractModel:
    """两阶段 QualityContract 聚合模型（Contract v1.0.0）。"""

    error_table_name: str
    raw_value_storage_policy: RawValueStoragePolicy
    rules: list[QualityRuleModel]
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise ValueError(f"不支持的 schema_version: {self.schema_version}")
        if not self.error_table_name or len(self.error_table_name) > 128:
            raise ValueError("error_table_name 长度必须在 1..128 之间")

    def to_dict(self) -> dict[str, Any]:
        """序列化为符合 `quality_contract.schema.json` 的字典。"""
        return {
            "schema_version": self.schema_version,
            "error_table_name": self.error_table_name,
            "raw_value_storage_policy": self.raw_value_storage_policy.value,
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityContractModel:
        """从字典解析 QualityContract。"""
        return cls(
            schema_version=str(data.get("schema_version", "1.0.0")),
            error_table_name=str(data["error_table_name"]),
            raw_value_storage_policy=RawValueStoragePolicy(data["raw_value_storage_policy"]),
            rules=[QualityRuleModel.from_dict(r) for r in data.get("rules", [])],
        )


@dataclass(frozen=True)
class ErrorTableRecord:
    """写入错误表（`<target>__error`）的标准单行结构（ADR-004 §1）。"""

    execution_id: str
    rule_id: str
    error_code: str
    reason_summary: str
    raw_payload_redacted: dict[str, Any]
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """转换为错误表落盘字典。"""
        return {
            "execution_id": self.execution_id,
            "rule_id": self.rule_id,
            "error_code": self.error_code,
            "reason_summary": self.reason_summary,
            "raw_payload_redacted": self.raw_payload_redacted,
            "detected_at": self.detected_at,
        }


@dataclass(frozen=True)
class QualityRuleResultRecord:
    """单条规则执行统计结果（对应 `quality_rule_results` 表）。"""

    id: str
    organization_id: str
    project_id: str
    quality_report_id: str
    rule_id: str
    rule_type: str
    checked_rows: int
    failed_rows: int
    passed: bool
    detail_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectedRecordSummaryRecord:
    """按规则与错误码聚合的违规记录脱敏摘要（对应 `rejected_record_summaries` 表）。"""

    id: str
    organization_id: str
    project_id: str
    execution_id: str
    error_table_ref: str
    rule_id: str
    error_code: str
    count_rows: int
    sample_redacted_json: list[dict[str, Any]]
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class QualityReportRecord:
    """单次 ExecutionRun 的完整两阶段质量汇总报告（对应 `quality_reports` 表）。"""

    id: str
    organization_id: str
    project_id: str
    execution_id: str
    contract_id: str
    validated_rows: int
    valid_rows: int
    error_rows: int
    reject_rate: float
    passed: bool
    failure_reasons_json: list[dict[str, Any]]
    rule_results: list[QualityRuleResultRecord] = field(default_factory=list)
    rejected_summaries: list[RejectedRecordSummaryRecord] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
