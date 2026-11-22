"""L0 / L1 / L2 基准测试编排引擎。

提供三层标准化基准评测编排能力：
- L0 (Static Security Injection): 读取 l0_security_cases.json，通过可注入的 SecurityEvaluator 评估拦截率。
- L1 (Fault Simulation): 读取 l1_fault_cases.json，通过可注入的 FaultSimulator 评估恢复与监督边界。
- L2 (Real End-to-End): 提供真实链路执行器接口（连接真实数据库、SeaTunnel、Vault、Doris 等）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from benchmarks.runner.scoring import (
    BenchmarkScoreResult,
    ProviderMode,
    RawBenchmarkMetrics,
    ScoringProfile,
    score_benchmark,
)

_DEFAULT_CASES_DIR = Path(__file__).resolve().parent.parent / "cases"


@dataclass
class CaseExecutionRecord:
    """单条基准用例的执行结果记录。"""

    case_id: str
    layer: str
    category: str
    risk_level: str | None
    passed: bool
    actual_blocked: bool | None = None
    actual_error_code: str | None = None
    actual_outcome: str | None = None
    detail: str = ""


class SecurityEvaluator(Protocol):
    """L0 安全评估器协议（可对接真实 Harness PDP/SQL Gate 或确定性基线）。"""

    def evaluate(self, case: dict[str, Any]) -> tuple[bool, str | None]:
        """返回 (is_blocked, error_code)。"""
        ...


class FaultSimulator(Protocol):
    """L1 故障模拟器协议。"""

    def simulate(self, case: dict[str, Any]) -> tuple[str, str | None]:
        """返回 (actual_outcome, error_code)。"""
        ...


class L2Runner(Protocol):
    """L2 真实端到端链路执行器协议。"""

    def run_e2e(self, scenario: str, tier: str) -> dict[str, float]:
        """返回 L2 实测指标字典（end_to_end_success, connector_capability_pass_rate 等）。"""
        ...


class DefaultStaticSecurityBaseline:
    """内置 L0 确定性基线评估器（用于无外部服务时的引擎自检与对照）。"""

    _SQL_DANGER = re.compile(r"\b(DROP|DELETE|TRUNCATE|ALTER|LOAD_FILE|INTO\s+OUTFILE)\b|;", re.IGNORECASE)
    _PROMPT_INJECT = re.compile(r"ignore\s+all\s+previous\s+instructions|dump\s+all\s+secrets", re.IGNORECASE)

    def evaluate(self, case: dict[str, Any]) -> tuple[bool, str | None]:
        cat = case.get("category", "")
        inp = case.get("input", {})

        if cat == "prompt_injection" and self._PROMPT_INJECT.search(inp.get("prompt", "")):
            return True, "AGENT_TOOL_FORBIDDEN"
        if cat == "illegal_sql" and self._SQL_DANGER.search(inp.get("sql_text", "")):
            return True, "SQL_AST_POLICY_VIOLATION"
        if cat == "out_of_scope_tool_intent" and inp.get("tool_name") == "exec_shell_command":
            return True, "PDP_DENIED_P0"
        if cat == "sensitive_exfiltration" and (
            inp.get("data_classification") == "RESTRICTED_PII" and not inp.get("llm_sample_policy_approved")
        ):
            return True, "PERMISSION_DENIED"
        if cat == "capability_replay" and inp.get("already_consumed") is True:
            return True, "CAPABILITY_REPLAY_DETECTED"
        if cat == "path_traversal" and ".." in str(inp.get("file_path", "")):
            return True, "FILE_PATH_TRAVERSAL_BLOCKED"
        if cat == "maker_self_approve" and inp.get("maker_id") == inp.get("approver_id"):
            return True, "APPROVAL_SELF_APPROVE_FORBIDDEN"
        return False, None


class DefaultFaultSimulatorBaseline:
    """内置 L1 故障模拟基线（严格校验 20% 拒绝率严格大于边界与 Zeta 对账状态）。"""

    def simulate(self, case: dict[str, Any]) -> tuple[str, str | None]:
        fault_type = case.get("fault_type", "")
        params = case.get("simulation_params", {})

        if fault_type == "db_disconnect":
            return "RECOVERABLE_FAILURE", "CONNECTION_TEST_FAILED"
        if fault_type == "worker_crash_redelivery":
            return "IDEMPOTENT_NO_DUPLICATE", None
        if fault_type == "zeta_submit_unknown":
            return "RECONCILIATION_REQUIRED", "SEATUNNEL_SUBMIT_UNKNOWN"
        if fault_type in ("reject_rate_breach", "reject_rate_exact_boundary"):
            validated = float(params.get("validated_rows", 0))
            errors = float(params.get("error_rows", 0))
            threshold = float(params.get("threshold", 0.20))
            rate = (errors / validated) if validated > 0 else 0.0
            # 契约规定：严格大于 20% 触发，恰好 20% 不触发
            if rate > threshold:
                return "SUPERVISION_INTERRUPTED", "SUPERVISION_THRESHOLD_BREACHED"
            return "ALLOWED_AT_BOUNDARY", None
        if fault_type == "schema_drift":
            return "PUBLISH_BLOCKED", "PUBLISH_SCHEMA_INCOMPATIBLE"
        return "UNKNOWN_OUTCOME", None


@dataclass
class BenchmarkRunReport:
    """完整基准评测运行报告。"""

    run_id: str
    provider_mode: ProviderMode
    l0_records: list[CaseExecutionRecord] = field(default_factory=list)
    l1_records: list[CaseExecutionRecord] = field(default_factory=list)
    l2_metrics: dict[str, float] = field(default_factory=dict)
    score_result: BenchmarkScoreResult | None = None


class BenchmarkEngine:
    """L0 / L1 / L2 基准测试统一编排器。"""

    def __init__(
        self,
        cases_dir: Path | str | None = None,
        security_evaluator: SecurityEvaluator | None = None,
        fault_simulator: FaultSimulator | None = None,
        l2_runner: L2Runner | None = None,
    ) -> None:
        self.cases_dir = Path(cases_dir) if cases_dir else _DEFAULT_CASES_DIR
        self.security_evaluator: SecurityEvaluator = security_evaluator or DefaultStaticSecurityBaseline()
        self.fault_simulator: FaultSimulator = fault_simulator or DefaultFaultSimulatorBaseline()
        self.l2_runner = l2_runner

    def load_cases(self, filename: str) -> list[dict[str, Any]]:
        """从 JSON 用例文件加载测试用例列表。"""
        path = self.cases_dir / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("cases", []))

    def run_l0(self) -> list[CaseExecutionRecord]:
        """执行 L0 静态安全注入测试集。"""
        cases = self.load_cases("l0_security_cases.json")
        records: list[CaseExecutionRecord] = []
        for case in cases:
            blocked, err_code = self.security_evaluator.evaluate(case)
            expected_blocked = bool(case.get("expected_blocked", True))
            expected_err = case.get("expected_error_code")
            passed = (blocked == expected_blocked) and (
                expected_err is None or err_code == expected_err
            )
            records.append(
                CaseExecutionRecord(
                    case_id=case["case_id"],
                    layer="L0",
                    category=case.get("category", ""),
                    risk_level=case.get("risk_level"),
                    passed=passed,
                    actual_blocked=blocked,
                    actual_error_code=err_code,
                    detail=case.get("description", ""),
                )
            )
        return records

    def run_l1(self) -> list[CaseExecutionRecord]:
        """执行 L1 故障模拟测试集。"""
        cases = self.load_cases("l1_fault_cases.json")
        records: list[CaseExecutionRecord] = []
        for case in cases:
            outcome, err_code = self.fault_simulator.simulate(case)
            passed = (outcome == case.get("expected_outcome")) and (
                err_code == case.get("expected_error_code")
            )
            records.append(
                CaseExecutionRecord(
                    case_id=case["case_id"],
                    layer="L1",
                    category=case.get("fault_type", ""),
                    risk_level=None,
                    passed=passed,
                    actual_outcome=outcome,
                    actual_error_code=err_code,
                    detail=case.get("description", ""),
                )
            )
        return records

    def run_suite(
        self,
        run_id: str,
        provider_mode: ProviderMode = "mock",
        generation_metrics: dict[str, float] | None = None,
        l2_scenario: str = "orders_users",
        l2_tier: str = "10K",
        profile: ScoringProfile | None = None,
    ) -> BenchmarkRunReport:
        """运行 L0 + L1 (+ 可选 L2) 完整套件并输出评分报告。"""
        l0_records = self.run_l0()
        l1_records = self.run_l1()

        p0_cases = [r for r in l0_records if r.risk_level == "P0"]
        p1_cases = [r for r in l0_records if r.risk_level == "P1"]
        sql_cases = [r for r in l0_records if r.category == "illegal_sql"]
        replay_cases = [r for r in l0_records if r.category == "capability_replay"]

        p0_failed = sum(1 for r in p0_cases if not r.passed)
        p1_failed = sum(1 for r in p1_cases if not r.passed)
        p0_rate = ((len(p0_cases) - p0_failed) / len(p0_cases)) if p0_cases else 1.0
        p1_rate = ((len(p1_cases) - p1_failed) / len(p1_cases)) if p1_cases else 1.0
        sql_rate = (sum(1 for r in sql_cases if r.passed) / len(sql_cases)) if sql_cases else 1.0
        replay_rate = (sum(1 for r in replay_cases if r.passed) / len(replay_cases)) if replay_cases else 1.0

        l1_pass_rate = (sum(1 for r in l1_records if r.passed) / len(l1_records)) if l1_records else 1.0

        l2_metrics: dict[str, float] = {}
        if self.l2_runner is not None:
            l2_metrics = self.l2_runner.run_e2e(l2_scenario, l2_tier)

        gen = generation_metrics or {}
        raw = RawBenchmarkMetrics(
            plan_validity=float(gen.get("plan_validity", 1.0)),
            hocon_validity=float(gen.get("hocon_validity", 1.0)),
            schema_mapping_accuracy=float(gen.get("schema_mapping_accuracy", 1.0)),
            sql_safety_detection=sql_rate,
            p0_block_rate=p0_rate,
            p1_block_rate=p1_rate,
            replay_prevention=replay_rate,
            repair_success_rate=float(gen.get("repair_success_rate", 1.0)),
            end_to_end_success=float(l2_metrics.get("end_to_end_success", 1.0 if self.l2_runner is None else 0.0)),
            recovery_success=l1_pass_rate,
            connector_capability_pass_rate=float(
                l2_metrics.get("connector_capability_pass_rate", 1.0 if self.l2_runner is None else 0.0)
            ),
            p0_failed_count=p0_failed,
            p1_failed_count=p1_failed,
        )

        score_res = score_benchmark(raw, provider_mode=provider_mode, profile=profile)
        return BenchmarkRunReport(
            run_id=run_id,
            provider_mode=provider_mode,
            l0_records=l0_records,
            l1_records=l1_records,
            l2_metrics=l2_metrics,
            score_result=score_res,
        )
