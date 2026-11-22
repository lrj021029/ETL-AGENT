"""两阶段数据质量契约引擎（Two-Stage QualityContract Engine）。

严格遵循 `ADR-004` 与 `contracts/schemas/quality_contract.schema.json`（Contract v1.0.0）：
1. Stage 1（`STREAMING_ROW_SPLIT`）：面向 `NOT_NULL`、`DATA_TYPE`、`REGEX`、`RANGE`、`ENUM` 的流式行级校验与分流，
   合法行进入目标影子表（`<target>__shadow_<short_exec_id>`），违规行脱敏后进入错误表（`<target>__error`）。
2. Stage 2（`SHADOW_TABLE_VALIDATION`）：在 `VALIDATING` 阶段对影子表执行 `UNIQUE` 与受限只读 `SELECT` SQL 校验（经 AST 安全门禁）。
3. 汇总生成 `QualityReportRecord`、`QualityRuleResultRecord` 与脱敏错误摘要 `RejectedRecordSummaryRecord`。
"""

from backend.app.quality.engine import (
    RestrictedSqlAstValidator,
    Stage1SplitOutcome,
    TwoStageQualityEngine,
    redact_raw_payload,
    validate_quality_contract_schema,
)
from backend.app.quality.models import (
    ErrorTableRecord,
    QualityContractModel,
    QualityReportRecord,
    QualityRuleModel,
    QualityRuleResultRecord,
    QualityRuleType,
    QualitySeverity,
    QualityStage,
    RawValueStoragePolicy,
    RejectedRecordSummaryRecord,
    SqlAstPolicyViolationError,
)

__all__ = [
    "ErrorTableRecord",
    "QualityContractModel",
    "QualityReportRecord",
    "QualityRuleModel",
    "QualityRuleResultRecord",
    "QualityRuleType",
    "QualitySeverity",
    "QualityStage",
    "RawValueStoragePolicy",
    "RejectedRecordSummaryRecord",
    "RestrictedSqlAstValidator",
    "SqlAstPolicyViolationError",
    "Stage1SplitOutcome",
    "TwoStageQualityEngine",
    "redact_raw_payload",
    "validate_quality_contract_schema",
]
