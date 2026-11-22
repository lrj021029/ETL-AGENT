"""DeterministicGate：候选产物进入人工审批前的确定性硬门禁。

校验项：Schema 对齐、字段映射、SQL AST、策略一致性、HOCON 语法、Connector 能力、
QualityContract、RuntimeSupervisionContract、零明文密钥。
Gate 的规则集在构造时固定，Agent / RepairLoop 无法修改或放宽。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..hocon.generator import connector_family, needs_quality_split
from ..hocon.registry import ConnectorRegistry, check_plan_capabilities
from ..hocon.validator import (
    _INLINE_CRED_PARAM,
    _SECRET_PLACEHOLDER,
    _SENSITIVE_KEY,
    _URI_WITH_CREDENTIALS,
    secret_placeholders,
    validate_hocon,
)
from ..pipeline.models import (
    EtlPlan,
    QualityContract,
    RuntimeSupervisionContract,
    validate_against_contract,
)
from ..pipeline.tools import MetadataSnapshot, schema_alignment_diff
from ..sql_analysis.sandbox import (
    SqlAnalysisResult,
    analyze_select,
    quality_rule_profile,
    transformation_profile,
)
from .strategy import ExecutionStrategyCompiler

ERROR_CODE = "DETERMINISTIC_GATE_FAILED"


@dataclass(frozen=True)
class GateFailure:
    code: str
    path: str
    message: str


@dataclass
class GateResult:
    passed: bool
    failures: list[GateFailure] = field(default_factory=list)
    error_code: str | None = None
    analyses: dict[str, SqlAnalysisResult] = field(default_factory=dict)


def scan_plaintext_secrets(obj: Any, path: str = "") -> list[str]:
    """递归扫描明文密钥：敏感键对应非占位符的值，或值内嵌凭据 URI。"""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and _SENSITIVE_KEY.search(str(k)) and v and not _SECRET_PLACEHOLDER.match(v):
                if k not in ("placeholder", "secret_ref_id", "secret_version", "secret_refs"):
                    hits.append(p)
            else:
                hits += scan_plaintext_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += scan_plaintext_secrets(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        if _URI_WITH_CREDENTIALS.search(obj) or _INLINE_CRED_PARAM.search(obj):
            hits.append(path)
    return hits


class DeterministicGate:
    """规则集不可变；run() 为纯函数（无副作用）。"""

    def __init__(self, registry: ConnectorRegistry):
        self._registry = registry
        self._compiler = ExecutionStrategyCompiler()

    def run(
        self,
        plan_data: dict[str, Any],
        *,
        quality_data: dict[str, Any],
        supervision_data: dict[str, Any],
        metadata: MetadataSnapshot,
        hocon_text: str | None,
    ) -> GateResult:
        fails: list[GateFailure] = []
        res = GateResult(passed=False)

        def fail(code: str, path: str, msg: str) -> None:
            fails.append(GateFailure(code, path, msg))

        # 1. 明文密钥（在任何解析前先扫描原始数据）
        for p in scan_plaintext_secrets(plan_data) + scan_plaintext_secrets(quality_data):
            fail("PLAINTEXT_SECRET", p, "plaintext secret detected; use ${SECRET:NAME}")

        # 2. 契约结构（JSON Schema + Pydantic 双重校验）
        for schema, data in (
            ("etl_plan.schema.json", plan_data),
            ("quality_contract.schema.json", quality_data),
            ("runtime_supervision.schema.json", supervision_data),
        ):
            for msg in validate_against_contract(schema, data):
                fail("CONTRACT_SCHEMA_INVALID", schema, msg)
        try:
            plan = EtlPlan.model_validate(plan_data)
            quality = QualityContract.model_validate(quality_data)
            RuntimeSupervisionContract.model_validate(supervision_data)
        except ValidationError as e:
            for err in e.errors():
                fail("CONTRACT_MODEL_INVALID", ".".join(map(str, err["loc"])), err["msg"])
            return self._finish(res, fails)

        # 3. Schema / 字段映射对齐
        if plan.source.metadata_snapshot_id != metadata.snapshot_id:
            fail("METADATA_SNAPSHOT_MISMATCH", "source.metadata_snapshot_id", "plan not bound to provided snapshot")
        for issue in schema_alignment_diff(plan, metadata):
            fail(issue["code"], "field_mappings", str({k: v for k, v in issue.items() if k != "code"}))

        # 4. 同步模式所需参数
        if plan.sync_mode == "INCREMENTAL":
            for o in plan.source.objects:
                if not o.incremental_column:
                    fail("INCREMENTAL_COLUMN_MISSING", o.qualified, "incremental column required")
        if plan.sync_mode == "CDC" or plan.sink.write_mode == "UPSERT":
            if not plan.sink.primary_keys:
                fail("PRIMARY_KEY_MISSING", "sink.primary_keys", "primary key required for UPSERT/CDC")
        if (plan.sync_mode == "CDC") != (plan.publish_strategy.adapter == "CDC_STREAMING_UPSERT"):
            fail("PUBLISH_STRATEGY_MISMATCH", "publish_strategy.adapter", "CDC must use CDC_STREAMING_UPSERT")

        # 5. SQL AST（Transformation Profile）
        allowed: dict[str, set[str]] = {
            o.qualified.lower(): {c.lower() for c in meta.columns}
            for o in plan.source.objects
            if (meta := metadata.tables.get(o.qualified)) is not None
        }
        target_key = f"{plan.sink.target_schema}.{plan.sink.target_table}".lower()
        allowed[target_key] = {m.target_field.lower() for m in plan.field_mappings}
        default_schema = plan.source.objects[0].schema_name
        for step in plan.transformations:
            if step.kind != "SELECT_SQL":
                continue
            prof = transformation_profile(step.sql_dialect, default_schema, allowed)
            a = analyze_select(step.sql_text, prof)
            res.analyses[step.step_id] = a
            for v in a.violations:
                fail("SQL_AST_POLICY_VIOLATION", f"transformations.{step.step_id}", f"{v.code}: {v.detail}")
        for rule in quality.rules:
            if rule.rule_type == "RESTRICTED_SQL":
                if not rule.sql_text or not rule.sql_dialect:
                    fail("SQL_AST_POLICY_VIOLATION", f"quality.{rule.rule_id}", "sql_text/sql_dialect required")
                    continue
                prof = quality_rule_profile(rule.sql_dialect, default_schema, allowed)
                for v in analyze_select(rule.sql_text, prof).violations:
                    fail("SQL_AST_POLICY_VIOLATION", f"quality.{rule.rule_id}", f"{v.code}: {v.detail}")
            if rule.stage == "STREAMING_ROW_SPLIT" and rule.rule_type in ("UNIQUE", "RESTRICTED_SQL"):
                fail("QUALITY_STAGE_INVALID", f"quality.{rule.rule_id}", "UNIQUE/SQL rules belong to stage 2")

        # 6. 策略一致性：已解析策略必须等于确定性编译器重新推导的结果
        if not any(f.code == "SQL_AST_POLICY_VIOLATION" for f in fails):
            for step in plan.transformations:
                if step.kind == "SELECT_SQL":
                    d = self._compiler.decide(plan, step, res.analyses[step.step_id])
                    if (step.resolved_execution_strategy, tuple(step.strategy_reason_codes)) != (
                        d.strategy,
                        d.reason_codes,
                    ):
                        fail("STRATEGY_MISMATCH", f"transformations.{step.step_id}", f"expected {d.strategy}")

        # 7. Connector 能力
        for i in check_plan_capabilities(
            self._registry,
            source_connector=plan.source.connector_id,
            sink_connector=plan.sink.connector_id,
            sync_mode=plan.sync_mode,
            write_mode=plan.sink.write_mode,
            needs_quality_split=needs_quality_split(quality),
        ):
            fail(i.code, "capabilities", i.detail)

        # 8. HOCON 语法 + 占位符必须声明于 plan.secret_refs
        if not hocon_text:
            fail("HOCON_MISSING", "hocon", "hocon text required")
        else:
            for hi in validate_hocon(hocon_text):
                fail(hi.code, f"hocon.{hi.path}", hi.detail)
            declared = {s.placeholder for s in plan.secret_refs}
            for ph in sorted(secret_placeholders(hocon_text) - declared):
                fail("SECRET_REF_UNDECLARED", "secret_refs", f"{ph} not declared in plan.secret_refs")

        _ = connector_family
        return self._finish(res, fails)

    @staticmethod
    def _finish(res: GateResult, fails: list[GateFailure]) -> GateResult:
        res.failures = fails
        res.passed = not fails
        res.error_code = None if res.passed else ERROR_CODE
        return res
