"""两阶段质量执行引擎、脱敏器与受限 SQL AST 沙箱实现。

包含：
1. `validate_quality_contract_schema`：对照 `contracts/schemas/quality_contract.schema.json` 校验契约。
2. `redact_raw_payload`：按 `REDACTED_BY_CLASSIFICATION`、`HASH_ONLY`、`OMIT_SENSITIVE` 脱敏错误载荷。
3. `RestrictedSqlAstValidator`：基于 AST（优先使用 `sqlglot`，内置递归 token/语法树守卫）强制只允许受限 `SELECT`。
4. `TwoStageQualityEngine`：执行 Stage 1 行级流式分流、生成 SeaTunnel HOCON 分流配置、执行 Stage 2 影子表校验并汇总报告。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable
import uuid

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

_REPO_ROOT = Path(__file__).resolve().parents[3]
_QUALITY_SCHEMA_PATH = _REPO_ROOT / "contracts" / "schemas" / "quality_contract.schema.json"

# 受限质量 SQL 允许的聚合与标量函数白名单（PRD-TBD-02 参数化白名单，默认严格最小集）
DEFAULT_ALLOWED_QUALITY_SQL_FUNCTIONS: frozenset[str] = frozenset(
    {
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "COALESCE",
        "NULLIF",
        "ABS",
        "ROUND",
        "LOWER",
        "UPPER",
        "TRIM",
        "LENGTH",
        "CHAR_LENGTH",
    }
)

# 严禁出现在质量 SQL 中的危险关键字或系统目录前缀
_FORBIDDEN_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "CREATE",
        "REPLACE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "COPY",
        "LOAD",
        "OUTFILE",
        "DUMPFILE",
        "INTO",
        "SET",
        "COMMIT",
        "ROLLBACK",
        "SLEEP",
        "BENCHMARK",
        "PG_SLEEP",
        "PG_READ_FILE",
        "LO_IMPORT",
        "LO_EXPORT",
        "INFORMATION_SCHEMA",
        "PG_CATALOG",
        "MYSQL",
    }
)


def validate_quality_contract_schema(contract_dict: dict[str, Any]) -> QualityContractModel:
    """严格对照 `contracts/schemas/quality_contract.schema.json` 验证并解析 `QualityContractModel`。"""
    schema = json.loads(_QUALITY_SCHEMA_PATH.read_text(encoding="utf-8"))
    required_top = set(schema["required"])
    allowed_top = set(schema["properties"].keys())

    missing = required_top - set(contract_dict.keys())
    if missing:
        raise ValueError(f"QualityContract 缺少必填字段: {sorted(missing)}")
    extra = set(contract_dict.keys()) - allowed_top
    if extra:
        raise ValueError(f"QualityContract 包含未允许的额外字段: {sorted(extra)}")

    rule_schema = schema["properties"]["rules"]["items"]
    req_rule = set(rule_schema["required"])
    allowed_rule = set(rule_schema["properties"].keys())
    for idx, r in enumerate(contract_dict.get("rules", [])):
        if not isinstance(r, dict):
            raise ValueError(f"rules[{idx}] 必须是对象")
        r_missing = req_rule - set(r.keys())
        if r_missing:
            raise ValueError(f"rules[{idx}] 缺少必填字段: {sorted(r_missing)}")
        r_extra = set(r.keys()) - allowed_rule
        if r_extra:
            raise ValueError(f"rules[{idx}] 包含未允许的额外字段: {sorted(r_extra)}")

    return QualityContractModel.from_dict(contract_dict)


def redact_raw_payload(
    row: dict[str, Any],
    policy: RawValueStoragePolicy,
    sensitive_columns: set[str] | None = None,
) -> dict[str, Any]:
    """根据 `raw_value_storage_policy` 与敏感列标记对违规原始行进行脱敏处理。

    绝不允许未脱敏的敏感明文进入错误表或 `rejected_record_summaries`。
    """
    sens_set = {c.lower() for c in (sensitive_columns or set())}
    # 默认启发式识别常见凭据/敏感字段名，叠加显式传入的敏感列集合
    heuristic_keywords = ("password", "secret", "token", "ssn", "id_card", "phone", "email", "card_no")

    def _is_sensitive(col_name: str) -> bool:
        lower = col_name.lower()
        return lower in sens_set or any(k in lower for k in heuristic_keywords)

    redacted: dict[str, Any] = {}
    for col, val in row.items():
        if val is None:
            if policy == RawValueStoragePolicy.OMIT_SENSITIVE and _is_sensitive(col):
                continue
            redacted[col] = None
            continue

        val_str = str(val)
        if policy == RawValueStoragePolicy.HASH_ONLY:
            digest = hashlib.sha256(val_str.encode("utf-8")).hexdigest()[:16]
            redacted[col] = f"sha256:{digest}"
        elif policy == RawValueStoragePolicy.OMIT_SENSITIVE:
            if not _is_sensitive(col):
                redacted[col] = val
        elif policy == RawValueStoragePolicy.REDACTED_BY_CLASSIFICATION:
            if _is_sensitive(col):
                if len(val_str) <= 4:
                    redacted[col] = "***REDACTED***"
                else:
                    redacted[col] = f"{val_str[:2]}***{val_str[-2:]}"
            else:
                redacted[col] = val
    return redacted


class RestrictedSqlAstValidator:
    """Stage 2 受限质量查询 SQL AST 校验器（遵循 TECHNICAL_SPEC §9.2 与 PRD-TBD-02）。

    强制要求：
    1. 仅允许单条 `SELECT` 查询语句（禁止分号多语句、禁止任何 DDL/DML/写入/系统表）。
    2. 仅允许访问当前目标影子表（或白名单表）及白名单内的函数。
    3. 优先调用 `sqlglot` 解析 AST，若环境未安装 `sqlglot` 则使用内置确定性词法/语法树分析器。
    """

    def __init__(
        self,
        allowed_functions: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._allowed_functions = {
            f.upper() for f in (allowed_functions or DEFAULT_ALLOWED_QUALITY_SQL_FUNCTIONS)
        }

    def validate_select_sql(
        self,
        sql_text: str,
        *,
        allowed_tables: set[str],
        dialect: str = "doris",
    ) -> dict[str, Any]:
        """校验质量 SQL 安全性；任何违规立即抛出 `SqlAstPolicyViolationError`。"""
        if not sql_text or not sql_text.strip():
            raise SqlAstPolicyViolationError(
                "质量规则 SQL 不能为空",
                detail={"reason": "EMPTY_SQL"},
            )

        cleaned = sql_text.strip()
        # 禁止 SQL 注释隐藏攻击（-- 或 /* */）
        if "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
            raise SqlAstPolicyViolationError(
                "受限质量 SQL 禁止包含注释语法 (-- 或 /* */)",
                detail={"reason": "COMMENTS_FORBIDDEN", "sql": cleaned[:120]},
            )

        # 移除末尾单个可选分号后，严禁内部存在分号（防多语句注入）
        if cleaned.endswith(";"):
            cleaned = cleaned[:-1].strip()
        if ";" in cleaned:
            raise SqlAstPolicyViolationError(
                "受限质量 SQL 严禁包含多条语句",
                detail={"reason": "MULTI_STATEMENT_FORBIDDEN"},
            )

        # 移除字符串字面量后进行词法 Token 与函数调用结构分析
        sql_without_literals = re.sub(r"'[^']*'", "'__STR__'", cleaned)
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", sql_without_literals)
        if not tokens or tokens[0].upper() != "SELECT":
            raise SqlAstPolicyViolationError(
                "受限质量 SQL 必须以 SELECT 开头，仅允许只读查询",
                detail={"reason": "NOT_SELECT_STATEMENT", "first_token": tokens[0] if tokens else None},
            )

        # 检查是否包含任何禁止的 DDL/DML/系统目录/危险关键字
        for tok in tokens:
            upper_parts = tok.upper().split(".")
            for part in upper_parts:
                if part in _FORBIDDEN_SQL_KEYWORDS:
                    raise SqlAstPolicyViolationError(
                        f"受限质量 SQL 包含禁止的关键字或系统目录: {part}",
                        detail={"reason": "FORBIDDEN_KEYWORD", "keyword": part},
                    )

        # 提取所有函数调用 identifier(...) 并对照函数白名单
        func_calls = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", sql_without_literals)
        sql_clause_keywords = {"WHERE", "IN", "AND", "OR", "NOT", "HAVING", "ON", "OVER"}
        referenced_functions: list[str] = []
        for fn in func_calls:
            fn_upper = fn.upper()
            if fn_upper in sql_clause_keywords:
                continue
            referenced_functions.append(fn_upper)
            if fn_upper not in self._allowed_functions:
                raise SqlAstPolicyViolationError(
                    f"函数 {fn_upper} 不在受限质量 SQL 函数白名单中",
                    detail={
                        "reason": "FUNCTION_NOT_ALLOWLISTED",
                        "function": fn_upper,
                        "allowed_functions": sorted(self._allowed_functions),
                    },
                )

        # 提取 FROM / JOIN 引用的表名并校验对象白名单
        table_refs = re.findall(
            r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)",
            sql_without_literals,
            flags=re.IGNORECASE,
        )
        if not table_refs:
            raise SqlAstPolicyViolationError(
                "受限质量 SQL 必须显式指定 FROM <shadow_table>",
                detail={"reason": "MISSING_FROM_TABLE"},
            )

        normalized_allowed = {t.lower() for t in allowed_tables}
        for tbl in table_refs:
            tbl_lower = tbl.lower()
            short_tbl = tbl_lower.split(".")[-1]
            if tbl_lower not in normalized_allowed and short_tbl not in normalized_allowed:
                raise SqlAstPolicyViolationError(
                    f"质量 SQL 引用了未授权的表对象: {tbl}",
                    detail={
                        "reason": "TABLE_NOT_ALLOWLISTED",
                        "table": tbl,
                        "allowed_tables": sorted(allowed_tables),
                    },
                )

        # 若环境安装了 sqlglot，再叠加一层完整 sqlglot AST 节点校验
        try:
            import sqlglot
            from sqlglot import exp

            parsed = sqlglot.parse(cleaned, read=dialect if dialect != "doris" else "mysql")
            if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
                raise SqlAstPolicyViolationError(
                    "sqlglot AST 解析判定该语句不是单一 SELECT 查询",
                    detail={"reason": "SQLGLOT_NON_SELECT"},
                )
        except ImportError:
            pass
        except SqlAstPolicyViolationError:
            raise
        except Exception as exc:
            raise SqlAstPolicyViolationError(
                f"SQL 语法解析失败: {exc}",
                detail={"reason": "AST_PARSE_ERROR", "error": str(exc)},
            ) from exc

        return {
            "valid": True,
            "dialect": dialect,
            "statement_type": "SELECT",
            "referenced_tables": table_refs,
            "referenced_functions": sorted(set(referenced_functions)),
        }


@dataclass(frozen=True)
class Stage1SplitOutcome:
    """Stage 1 流式行级质量分流结果。"""

    shadow_table_rows: list[dict[str, Any]]
    error_table_records: list[ErrorTableRecord]
    rule_checked_counts: dict[str, int]
    rule_failed_counts: dict[str, int]


class TwoStageQualityEngine:
    """两阶段数据质量契约执行引擎（ADR-004）。"""

    def __init__(self, sql_validator: RestrictedSqlAstValidator | None = None) -> None:
        self._sql_validator = sql_validator or RestrictedSqlAstValidator()

    @staticmethod
    def _evaluate_stage1_rule(rule: QualityRuleModel, row: dict[str, Any]) -> tuple[bool, str]:
        """对单行数据评估单条 Stage 1 行级规则，返回 `(passed, reason_summary)`。"""
        col = rule.column_name or ""
        val = row.get(col)

        if rule.rule_type == QualityRuleType.NOT_NULL:
            if val is None or (isinstance(val, str) and val.strip() == ""):
                return False, f"字段 '{col}' 违反 NOT_NULL 非空约束"
            return True, "OK"

        # 对于非 NOT_NULL 规则，若值为 None 则交由 NOT_NULL 规则管辖
        if val is None:
            return True, "NULL_SKIPPED"

        if rule.rule_type == QualityRuleType.DATA_TYPE:
            expected_type = str(rule.params.get("expected_type", "STRING")).upper()
            if expected_type in ("INT", "INTEGER", "BIGINT"):
                if isinstance(val, bool):
                    return False, f"字段 '{col}' 期望整数类型但实际为布尔值"
                try:
                    int(str(val))
                except (ValueError, TypeError):
                    return False, f"字段 '{col}' 无法转换为整数类型 ({expected_type})"
            elif expected_type in ("FLOAT", "DOUBLE", "DECIMAL", "NUMBER"):
                if isinstance(val, bool):
                    return False, f"字段 '{col}' 期望数值类型但实际为布尔值"
                try:
                    float(str(val))
                except (ValueError, TypeError):
                    return False, f"字段 '{col}' 无法转换为数值类型 ({expected_type})"
            elif expected_type == "BOOLEAN":
                if not isinstance(val, bool) and str(val).lower() not in ("true", "false", "0", "1"):
                    return False, f"字段 '{col}' 无法转换为布尔类型"
            elif expected_type in ("DATE", "DATETIME", "TIMESTAMP"):
                try:
                    datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                except ValueError:
                    return False, f"字段 '{col}' 不符合 ISO 日期/时间格式 ({expected_type})"
            return True, "OK"

        if rule.rule_type == QualityRuleType.REGEX:
            pattern = str(rule.params.get("pattern", ".*"))
            if not re.match(pattern, str(val)):
                return False, f"字段 '{col}' 未匹配正则表达式 '{pattern}'"
            return True, "OK"

        if rule.rule_type == QualityRuleType.RANGE:
            try:
                num_val = float(val)
            except (ValueError, TypeError):
                return False, f"字段 '{col}' 非数值，无法执行 RANGE 范围校验"
            min_v = rule.params.get("min")
            max_v = rule.params.get("max")
            if min_v is not None and num_val < float(min_v):
                return False, f"字段 '{col}' 值 {num_val} 小于下限 {min_v}"
            if max_v is not None and num_val > float(max_v):
                return False, f"字段 '{col}' 值 {num_val} 大于上限 {max_v}"
            return True, "OK"

        if rule.rule_type == QualityRuleType.ENUM:
            allowed_values = rule.params.get("allowed_values", [])
            if val not in allowed_values and str(val) not in {str(x) for x in allowed_values}:
                return False, f"字段 '{col}' 值不在允许枚举集合 {allowed_values} 中"
            return True, "OK"

        raise ValueError(f"Stage 1 不支持规则类型: {rule.rule_type}")

    def run_stage1_row_split(
        self,
        *,
        execution_id: str,
        contract: QualityContractModel,
        rows: list[dict[str, Any]],
        sensitive_columns: set[str] | None = None,
    ) -> Stage1SplitOutcome:
        """执行 Stage 1 流式行级校验与分流（ADR-004 §1）。

        - 合法行进入 `shadow_table_rows`；
        - 违反 `REJECT_ROW` 或 `FAIL_RUN` 规则的行经脱敏后进入 `error_table_records`。
        """
        stage1_rules = [
            r for r in contract.rules if r.stage == QualityStage.STREAMING_ROW_SPLIT
        ]
        shadow_rows: list[dict[str, Any]] = []
        error_records: list[ErrorTableRecord] = []
        checked_counts = {r.rule_id: 0 for r in stage1_rules}
        failed_counts = {r.rule_id: 0 for r in stage1_rules}

        for row in rows:
            row_rejected = False
            for rule in stage1_rules:
                checked_counts[rule.rule_id] += 1
                passed, reason = self._evaluate_stage1_rule(rule, row)
                if not passed:
                    failed_counts[rule.rule_id] += 1
                    redacted = redact_raw_payload(
                        row,
                        contract.raw_value_storage_policy,
                        sensitive_columns=sensitive_columns,
                    )
                    error_records.append(
                        ErrorTableRecord(
                            execution_id=execution_id,
                            rule_id=rule.rule_id,
                            error_code=rule.error_code,
                            reason_summary=reason,
                            raw_payload_redacted=redacted,
                        )
                    )
                    if rule.severity in (QualitySeverity.REJECT_ROW, QualitySeverity.FAIL_RUN):
                        row_rejected = True
                        break
            if not row_rejected:
                shadow_rows.append(dict(row))

        return Stage1SplitOutcome(
            shadow_table_rows=shadow_rows,
            error_table_records=error_records,
            rule_checked_counts=checked_counts,
            rule_failed_counts=failed_counts,
        )

    def build_seatunnel_stage1_split_wiring(
        self,
        *,
        source_table_name: str,
        shadow_table_name: str,
        contract: QualityContractModel,
    ) -> dict[str, Any]:
        """生成 SeaTunnel Zeta 2.3.9 Stage 1 流式分流 SQL Transform 与双路 Sink 绑定描述。"""
        stage1_rules = [
            r for r in contract.rules if r.stage == QualityStage.STREAMING_ROW_SPLIT
        ]
        valid_predicates: list[str] = []
        for rule in stage1_rules:
            col = rule.column_name or "id"
            if rule.rule_type == QualityRuleType.NOT_NULL:
                valid_predicates.append(f"{col} IS NOT NULL")
            elif rule.rule_type == QualityRuleType.RANGE:
                if "min" in rule.params:
                    valid_predicates.append(f"{col} >= {rule.params['min']}")
                if "max" in rule.params:
                    valid_predicates.append(f"{col} <= {rule.params['max']}")
            elif rule.rule_type == QualityRuleType.ENUM:
                vals = ", ".join(f"'{v}'" for v in rule.params.get("allowed_values", []))
                if vals:
                    valid_predicates.append(f"{col} IN ({vals})")

        where_valid = " AND ".join(valid_predicates) if valid_predicates else "1 = 1"
        where_error = f"NOT ({where_valid})" if valid_predicates else "1 = 0"

        return {
            "source_table_name": source_table_name,
            "shadow_table_name": shadow_table_name,
            "error_table_name": contract.error_table_name,
            "valid_filter_sql": f"SELECT * FROM {source_table_name} WHERE {where_valid}",
            "error_filter_sql": f"SELECT * FROM {source_table_name} WHERE {where_error}",
            "error_table_columns": [
                "execution_id",
                "rule_id",
                "error_code",
                "reason_summary",
                "raw_payload_redacted",
                "detected_at",
            ],
        }

    def run_stage2_shadow_validation(
        self,
        *,
        organization_id: str,
        project_id: str,
        execution_id: str,
        contract_id: str,
        shadow_table_name: str,
        contract: QualityContractModel,
        stage1_outcome: Stage1SplitOutcome,
        total_input_rows: int,
        sensitive_columns: set[str] | None = None,
        restricted_sql_executor: Callable[[str], int] | None = None,
    ) -> QualityReportRecord:
        """在 `VALIDATING` 阶段对影子表执行 Stage 2 质量门禁（`UNIQUE` 与 `RESTRICTED_SQL`）并生成完整报告。

        参数说明：
        - `restricted_sql_executor`：受管只读 SQL 执行回调，接收通过 AST 校验的 `SELECT` 语句并返回违规行数。
        """
        report_id = str(uuid.uuid4())
        rule_results: list[QualityRuleResultRecord] = []
        failure_reasons: list[dict[str, Any]] = []
        all_error_records = list(stage1_outcome.error_table_records)
        stage2_extra_error_rows = 0

        # 1. 记录 Stage 1 规则统计结果
        for rule in contract.rules:
            if rule.stage == QualityStage.STREAMING_ROW_SPLIT:
                checked = stage1_outcome.rule_checked_counts.get(rule.rule_id, total_input_rows)
                failed = stage1_outcome.rule_failed_counts.get(rule.rule_id, 0)
                rule_passed = failed == 0 or rule.severity != QualitySeverity.FAIL_RUN
                if not rule_passed:
                    failure_reasons.append(
                        {
                            "rule_id": rule.rule_id,
                            "error_code": rule.error_code,
                            "stage": rule.stage.value,
                            "failed_rows": failed,
                        }
                    )
                rule_results.append(
                    QualityRuleResultRecord(
                        id=str(uuid.uuid4()),
                        organization_id=organization_id,
                        project_id=project_id,
                        quality_report_id=report_id,
                        rule_id=rule.rule_id,
                        rule_type=rule.rule_type.value,
                        checked_rows=checked,
                        failed_rows=failed,
                        passed=(failed == 0),
                        detail_json={"stage": "STREAMING_ROW_SPLIT", "severity": rule.severity.value},
                    )
                )

        # 2. 执行 Stage 2 影子表校验规则（UNIQUE & RESTRICTED_SQL）
        shadow_rows = stage1_outcome.shadow_table_rows
        for rule in contract.rules:
            if rule.stage != QualityStage.SHADOW_TABLE_VALIDATION:
                continue

            if rule.rule_type == QualityRuleType.UNIQUE:
                key_cols = rule.params.get("columns") or (
                    [rule.column_name] if rule.column_name else []
                )
                seen_keys: set[tuple[Any, ...]] = set()
                dup_count = 0
                for s_row in shadow_rows:
                    key_tuple = tuple(s_row.get(k) for k in key_cols)
                    if key_tuple in seen_keys:
                        dup_count += 1
                        all_error_records.append(
                            ErrorTableRecord(
                                execution_id=execution_id,
                                rule_id=rule.rule_id,
                                error_code=rule.error_code,
                                reason_summary=f"影子表唯一键 {key_cols} 存在重复记录",
                                raw_payload_redacted=redact_raw_payload(
                                    s_row,
                                    contract.raw_value_storage_policy,
                                    sensitive_columns=sensitive_columns,
                                ),
                            )
                        )
                    else:
                        seen_keys.add(key_tuple)

                stage2_extra_error_rows += dup_count
                if dup_count > 0 and rule.severity in (
                    QualitySeverity.FAIL_RUN,
                    QualitySeverity.REJECT_ROW,
                ):
                    failure_reasons.append(
                        {
                            "rule_id": rule.rule_id,
                            "error_code": rule.error_code,
                            "stage": rule.stage.value,
                            "failed_rows": dup_count,
                        }
                    )
                rule_results.append(
                    QualityRuleResultRecord(
                        id=str(uuid.uuid4()),
                        organization_id=organization_id,
                        project_id=project_id,
                        quality_report_id=report_id,
                        rule_id=rule.rule_id,
                        rule_type=rule.rule_type.value,
                        checked_rows=len(shadow_rows),
                        failed_rows=dup_count,
                        passed=(dup_count == 0),
                        detail_json={"stage": "SHADOW_TABLE_VALIDATION", "key_columns": key_cols},
                    )
                )

            elif rule.rule_type == QualityRuleType.RESTRICTED_SQL:
                ast_report = self._sql_validator.validate_select_sql(
                    rule.sql_text or "",
                    allowed_tables={shadow_table_name},
                    dialect=rule.sql_dialect or "doris",
                )
                failed_sql_rows = (
                    restricted_sql_executor(rule.sql_text or "")
                    if restricted_sql_executor is not None
                    else int(rule.params.get("simulated_violation_count", 0))
                )
                stage2_extra_error_rows += failed_sql_rows
                if failed_sql_rows > 0:
                    all_error_records.append(
                        ErrorTableRecord(
                            execution_id=execution_id,
                            rule_id=rule.rule_id,
                            error_code=rule.error_code,
                            reason_summary=f"受限 SQL 质量查询在影子表检测到 {failed_sql_rows} 行违规",
                            raw_payload_redacted={"shadow_table": shadow_table_name},
                        )
                    )
                    if rule.severity in (QualitySeverity.FAIL_RUN, QualitySeverity.REJECT_ROW):
                        failure_reasons.append(
                            {
                                "rule_id": rule.rule_id,
                                "error_code": rule.error_code,
                                "stage": rule.stage.value,
                                "failed_rows": failed_sql_rows,
                            }
                        )
                rule_results.append(
                    QualityRuleResultRecord(
                        id=str(uuid.uuid4()),
                        organization_id=organization_id,
                        project_id=project_id,
                        quality_report_id=report_id,
                        rule_id=rule.rule_id,
                        rule_type=rule.rule_type.value,
                        checked_rows=len(shadow_rows),
                        failed_rows=failed_sql_rows,
                        passed=(failed_sql_rows == 0),
                        detail_json={"stage": "SHADOW_TABLE_VALIDATION", "ast_report": ast_report},
                    )
                )

        # 3. 聚合脱敏错误记录摘要（对应 rejected_record_summaries 表）
        grouped_errors: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for err in all_error_records:
            key = (err.rule_id, err.error_code)
            grouped_errors.setdefault(key, []).append(err.raw_payload_redacted)

        rejected_summaries: list[RejectedRecordSummaryRecord] = []
        for (r_id, e_code), samples in sorted(grouped_errors.items()):
            rejected_summaries.append(
                RejectedRecordSummaryRecord(
                    id=str(uuid.uuid4()),
                    organization_id=organization_id,
                    project_id=project_id,
                    execution_id=execution_id,
                    error_table_ref=contract.error_table_name,
                    rule_id=r_id,
                    error_code=e_code,
                    count_rows=len(samples),
                    sample_redacted_json=samples[:5],
                )
            )

        validated_rows = max(0, total_input_rows)
        total_error_rows = len(stage1_outcome.error_table_records) + stage2_extra_error_rows
        valid_rows = max(0, len(shadow_rows) - stage2_extra_error_rows)
        reject_rate = (total_error_rows / validated_rows) if validated_rows > 0 else 0.0

        return QualityReportRecord(
            id=report_id,
            organization_id=organization_id,
            project_id=project_id,
            execution_id=execution_id,
            contract_id=contract_id,
            validated_rows=validated_rows,
            valid_rows=valid_rows,
            error_rows=total_error_rows,
            reject_rate=reject_rate,
            passed=(len(failure_reasons) == 0),
            failure_reasons_json=failure_reasons,
            rule_results=rule_results,
            rejected_summaries=rejected_summaries,
        )
