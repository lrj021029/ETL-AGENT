"""LangGraph 主 StateGraph + 7 个子图。

流程：ParseIntent -> MetadataInspection -> BuildEtlPlan -> SQLAnalysisAndStrategyCompile
      -> HoconGeneration -> DeterministicGate -> (通过) Finalize
                                               -> (失败且未耗尽) Repair -> SQLAnalysisAndStrategyCompile
                                               -> (耗尽) 交还 Maker（REPAIR_LOOP_EXHAUSTED）

安全不变量：
* Gate 与 SQL 策略 Profile 由 AgentDeps 在构建时注入，不进入 state，RepairLoop 无法触及；
* LLM 只产出语义部分（字段映射 / SQL 文本）；连接、Connector、密钥引用、执行策略均由确定性代码填写；
* Agent 仅持有只读工具。
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from ..artifacts.manifest import build_pipeline_version
from ..compiler.gate import DeterministicGate
from ..compiler.strategy import ExecutionStrategyCompiler
from ..hocon.generator import connector_family, generate_hocon
from ..hocon.registry import ConnectorRegistry
from ..hocon.validator import HoconValidationError, secret_placeholders
from ..pipeline.models import EtlPlan, QualityContract, default_supervision
from ..pipeline.tools import MetadataSnapshot, build_dag
from ..sql_analysis.sandbox import analyze_select, transformation_profile
from .context import AgentContext, TenantAccessDenied
from .providers import ProviderGateway
from .tools import ReadOnlyToolRegistry


class AgentState(TypedDict, total=False):
    owner: dict[str, str]
    request: dict[str, Any]  # {"prompt", "hints", "bindings"}
    intent: dict[str, Any]
    missing: list[str]
    metadata: dict[str, Any]
    plan_parts: dict[str, Any]
    plan: dict[str, Any]
    quality: dict[str, Any]
    supervision: dict[str, Any]
    analyses_ok: bool
    hocon: str
    gate: dict[str, Any]
    repair_attempts: int
    status: str
    error_code: str | None
    candidate: dict[str, Any]
    handback: dict[str, Any]
    llm_audit: Annotated[list[dict[str, Any]], operator.add]


@dataclass
class AgentDeps:
    gateway: ProviderGateway
    tools: ReadOnlyToolRegistry
    registry: ConnectorRegistry
    gate: DeterministicGate
    max_repair_attempts: int = 3


def compute_missing(intent: dict[str, Any]) -> list[str]:
    miss = [k for k in ("source_table", "sink_table", "sync_mode") if not intent.get(k)]
    write_mode = intent.get("write_mode") or ("UPSERT" if intent.get("sync_mode") == "CDC" else "SHADOW_SWAP")
    if intent.get("sync_mode") == "INCREMENTAL" and not intent.get("incremental_column"):
        miss.append("incremental_column")
    if (write_mode == "UPSERT" or intent.get("sync_mode") == "CDC") and not intent.get("primary_keys"):
        miss.append("primary_keys")
    return miss


def _ctx_from(d: dict[str, str]) -> AgentContext:
    return AgentContext(**d)


# ---------------------------------------------------------------- 子图 1: ParseIntent
def parse_intent_subgraph(deps: AgentDeps):
    def extract(state: AgentState) -> dict[str, Any]:
        req = state["request"]
        out, audit = deps.gateway.call("parse_intent", {"prompt": req.get("prompt", ""), "hints": req.get("hints", {})})
        intent = dict(out)
        return {"intent": intent, "missing": compute_missing(intent), "llm_audit": [audit]}

    def clarify(state: AgentState) -> dict[str, Any]:
        # 中断并持久化 checkpoint；恢复值必须携带调用者身份，二次校验后才合并答案
        answer = interrupt({"type": "CLARIFICATION_REQUIRED", "missing": state["missing"], "thread_id": state["owner"]["thread_id"]})
        if _ctx_from(answer["ctx"]).owner() != state["owner"]:
            raise TenantAccessDenied("resume identity does not match thread owner")
        intent = {**state["intent"], **answer.get("answers", {})}
        return {"intent": intent, "missing": compute_missing(intent), "status": "RESUMED"}

    def route(state: AgentState) -> str:
        return "clarify" if state["missing"] else END

    g = StateGraph(AgentState)
    g.add_node("extract", extract)
    g.add_node("clarify", clarify)
    g.add_edge(START, "extract")
    g.add_conditional_edges("extract", route, {"clarify": "clarify", END: END})
    g.add_conditional_edges("clarify", route, {"clarify": "clarify", END: END})
    return g.compile()


# ---------------------------------------------------------------- 子图 2: MetadataInspection
def metadata_subgraph(deps: AgentDeps):
    def inspect(state: AgentState) -> dict[str, Any]:
        owner = _ctx_from(state["owner"])
        b = state["request"]["bindings"]
        snap: MetadataSnapshot = deps.tools.call(
            "inspect_metadata_snapshot", owner, b["source"]["connection_id"], b["source"]["metadata_snapshot_id"]
        )
        return {"metadata": snap.model_dump(mode="json")}

    g = StateGraph(AgentState)
    g.add_node("inspect", inspect)
    g.add_edge(START, "inspect")
    g.add_edge("inspect", END)
    return g.compile()


# ---------------------------------------------------------------- 确定性组装
def _llm_payload(state: AgentState) -> dict[str, Any]:
    intent = state["intent"]
    tables = {
        k: {"columns": v["columns"], "primary_keys": v["primary_keys"], "sensitive_columns": v["sensitive_columns"]}
        for k, v in state["metadata"]["tables"].items()
    }
    fam = connector_family(state["request"]["bindings"]["source"]["connector_id"])
    return {"intent": intent, "metadata": {"tables": tables}, "dialect": fam}


def assemble_plan(state: AgentState, parts: dict[str, Any]) -> dict[str, Any]:
    """连接 / Connector / 写入模式 / 发布策略全部由确定性代码填写，LLM 只贡献 parts。"""
    intent, b = state["intent"], state["request"]["bindings"]
    sschema, stable = intent["source_table"].split(".", 1)
    tschema, ttable = intent["sink_table"].split(".", 1)
    sync = intent["sync_mode"]
    write_mode = intent.get("write_mode") or ("UPSERT" if sync == "CDC" else "SHADOW_SWAP")
    pks = list(intent.get("primary_keys") or [])
    sink_family = connector_family(b["sink"]["connector_id"])
    if sync == "CDC":
        adapter = "CDC_STREAMING_UPSERT"
    elif sink_family == "doris" and write_mode == "SHADOW_SWAP":
        adapter = "DORIS_ATOMIC_SWAP"
    else:
        adapter = "STAGING_INSERT"
    return {
        "schema_version": "1.0.0",
        "plan_name": str(parts.get("plan_name") or f"sync_{stable}")[:128],
        "sync_mode": sync,
        "source": {
            "connection_id": b["source"]["connection_id"],
            "connection_version": b["source"]["connection_version"],
            "connector_id": b["source"]["connector_id"],
            "metadata_snapshot_id": b["source"]["metadata_snapshot_id"],
            "objects": [
                {
                    "schema_name": sschema,
                    "table_name": stable,
                    "primary_keys": pks,
                    "incremental_column": intent.get("incremental_column"),
                }
            ],
        },
        "sink": {
            "connection_id": b["sink"]["connection_id"],
            "connection_version": b["sink"]["connection_version"],
            "connector_id": b["sink"]["connector_id"],
            "target_schema": tschema,
            "target_table": ttable,
            "write_mode": write_mode,
            "primary_keys": pks,
        },
        "field_mappings": parts.get("field_mappings", []),
        "transformations": parts.get("transformations", []),
        "publish_strategy": {"adapter": adapter, "require_schema_compat_gate": True, "retain_previous_version": True},
        "secret_refs": [],  # 由 HoconGeneration 子图依据实际占位符确定性填写，忽略任何 LLM 输入
    }


def default_quality(plan: dict[str, Any]) -> dict[str, Any]:
    rules = []
    for m in plan.get("field_mappings", []):
        if m.get("nullable") is False:
            rules.append(
                {
                    "rule_id": f"nn_{m['target_field']}"[:64],
                    "rule_type": "NOT_NULL",
                    "stage": "STREAMING_ROW_SPLIT",
                    "column_name": m["target_field"],
                    "error_code": "DQ_NOT_NULL",
                    "severity": "REJECT_ROW",
                    "params": {},
                }
            )
    for pk in plan.get("sink", {}).get("primary_keys", []):
        rules.append(
            {
                "rule_id": f"uq_{pk}"[:64],
                "rule_type": "UNIQUE",
                "stage": "SHADOW_TABLE_VALIDATION",
                "column_name": pk,
                "error_code": "DQ_UNIQUE",
                "severity": "FAIL_RUN",
                "params": {},
            }
        )
    return {
        "schema_version": "1.0.0",
        "error_table_name": f"{plan.get('sink', {}).get('target_table', 'target')}_dq_errors"[:128],
        "raw_value_storage_policy": "REDACTED_BY_CLASSIFICATION",
        "rules": rules,
    }


# ---------------------------------------------------------------- 子图 3: BuildEtlPlan
def build_plan_subgraph(deps: AgentDeps):
    def build(state: AgentState) -> dict[str, Any]:
        parts, audit = deps.gateway.call("build_plan", _llm_payload(state))
        plan = assemble_plan(state, parts)
        return {
            "plan_parts": parts,
            "plan": plan,
            "quality": default_quality(plan),
            "supervision": default_supervision().model_dump(mode="json"),
            "llm_audit": [audit],
        }

    g = StateGraph(AgentState)
    g.add_node("build", build)
    g.add_edge(START, "build")
    g.add_edge("build", END)
    return g.compile()


# ---------------------------------------------------------------- 子图 4: SQLAnalysisAndStrategyCompile
def sql_strategy_subgraph(deps: AgentDeps):
    compiler = ExecutionStrategyCompiler()

    def analyze_and_compile(state: AgentState) -> dict[str, Any]:
        try:
            plan = EtlPlan.model_validate(state["plan"])
        except ValidationError:
            return {"analyses_ok": False}  # 交由 Gate 报告
        meta = MetadataSnapshot.model_validate(state["metadata"])
        allowed = {k: {c.lower() for c in v.columns} for k, v in meta.tables.items() if k in {o.qualified for o in plan.source.objects}}
        allowed[f"{plan.sink.target_schema}.{plan.sink.target_table}".lower()] = {m.target_field.lower() for m in plan.field_mappings}
        analyses = {}
        for step in plan.transformations:
            if step.kind == "SELECT_SQL":
                prof = transformation_profile(step.sql_dialect, plan.source.objects[0].schema_name, allowed)
                analyses[step.step_id] = analyze_select(step.sql_text, prof)
        if not all(a.ok for a in analyses.values()):
            return {"analyses_ok": False}
        compiled = compiler.compile(plan, analyses)
        return {"plan": compiled.to_dict(), "analyses_ok": True}

    g = StateGraph(AgentState)
    g.add_node("analyze_and_compile", analyze_and_compile)
    g.add_edge(START, "analyze_and_compile")
    g.add_edge("analyze_and_compile", END)
    return g.compile()


# ---------------------------------------------------------------- 子图 5: HoconGeneration
def hocon_subgraph(deps: AgentDeps):
    def generate(state: AgentState) -> dict[str, Any]:
        if not state.get("analyses_ok"):
            return {"hocon": ""}
        plan = EtlPlan.model_validate(state["plan"])
        try:
            text = generate_hocon(plan, deps.registry, QualityContract.model_validate(state["quality"]))
        except (HoconValidationError, ValidationError):
            return {"hocon": ""}  # Gate 会重新报告具体原因
        # 依据 HOCON 中实际出现的占位符确定性填写 secret_refs
        b = state["request"]["bindings"]
        refs = []
        for ph in sorted(secret_placeholders(text)):
            side = b["source"] if ph.startswith("${SECRET:SRC_") else b["sink"]
            refs.append({"placeholder": ph, "secret_ref_id": side["secret_ref_id"], "secret_version": side["secret_version"]})
        new_plan = {**state["plan"], "secret_refs": refs}
        return {"plan": new_plan, "hocon": text}

    g = StateGraph(AgentState)
    g.add_node("generate", generate)
    g.add_edge(START, "generate")
    g.add_edge("generate", END)
    return g.compile()


# ---------------------------------------------------------------- 子图 6: DeterministicGate
def gate_subgraph(deps: AgentDeps):
    def run_gate(state: AgentState) -> dict[str, Any]:
        res = deps.gate.run(
            state["plan"],
            quality_data=state["quality"],
            supervision_data=state["supervision"],
            metadata=MetadataSnapshot.model_validate(state["metadata"]),
            hocon_text=state.get("hocon"),
        )
        return {
            "gate": {
                "passed": res.passed,
                "error_code": res.error_code,
                "failures": [{"code": f.code, "path": f.path, "message": f.message} for f in res.failures],
            }
        }

    g = StateGraph(AgentState)
    g.add_node("run_gate", run_gate)
    g.add_edge(START, "run_gate")
    g.add_edge("run_gate", END)
    return g.compile()


# ---------------------------------------------------------------- 子图 7: RepairSubgraph
def repair_subgraph(deps: AgentDeps):
    def repair(state: AgentState) -> dict[str, Any]:
        attempts = state.get("repair_attempts", 0) + 1
        failures = state["gate"]["failures"]  # 仅 code/path/message，不含策略对象
        ctx = _llm_payload(state)
        parts, audit = deps.gateway.call(
            "repair_plan", {"plan_parts": state["plan_parts"], "failures": failures, "context": ctx}
        )
        plan = assemble_plan(state, parts)  # 连接 / 写入模式等仍由确定性代码重建
        return {
            "repair_attempts": attempts,
            "plan_parts": parts,
            "plan": plan,
            "quality": default_quality(plan),
            "llm_audit": [audit],
        }

    g = StateGraph(AgentState)
    g.add_node("repair", repair)
    g.add_edge(START, "repair")
    g.add_edge("repair", END)
    return g.compile()


# ---------------------------------------------------------------- 主图
def build_agent_graph(deps: AgentDeps, checkpointer: Any = None):
    def finalize(state: AgentState) -> dict[str, Any]:
        plan = state["plan"]
        sql_hashes = {t["step_id"]: _sql_hash(t["sql_text"]) for t in plan["transformations"]}
        pv = build_pipeline_version(
            pipeline_id=f"pipe_{state['owner']['thread_id']}",
            revision=1,
            plan=plan,
            quality_contract=state["quality"],
            supervision_contract=state["supervision"],
            hocon_text=state["hocon"],
            sql_hashes=sql_hashes,
            secret_refs=plan["secret_refs"],
        )
        info = state["llm_audit"][-1] if state.get("llm_audit") else {}
        return {
            "status": "CANDIDATE_READY",
            "error_code": None,
            "candidate": {
                "plan": plan,
                "quality_contract": state["quality"],
                "supervision_contract": state["supervision"],
                "hocon": state["hocon"],
                "dag": build_dag(EtlPlan.model_validate(plan)),
                "manifest": pv.manifest.model_dump(mode="json"),
                "gate": state["gate"],
                "repair_attempts": state.get("repair_attempts", 0),
                "is_mock_llm": bool(info.get("is_mock_llm", False)),
            },
        }

    def exhausted(state: AgentState) -> dict[str, Any]:
        return {
            "status": "HANDED_BACK_TO_MAKER",
            "error_code": "REPAIR_LOOP_EXHAUSTED",
            "handback": {
                "attempts": state.get("repair_attempts", 0),
                "max_repair_attempts": deps.max_repair_attempts,
                "failures": state["gate"]["failures"],
                "gate_rules_relaxed": False,
            },
        }

    def after_gate(state: AgentState) -> str:
        if state["gate"]["passed"]:
            return "finalize"
        if state.get("repair_attempts", 0) < deps.max_repair_attempts:
            return "repair"
        return "exhausted"

    g = StateGraph(AgentState)
    g.add_node("ParseIntentSubgraph", parse_intent_subgraph(deps))
    g.add_node("MetadataInspectionSubgraph", metadata_subgraph(deps))
    g.add_node("BuildEtlPlanSubgraph", build_plan_subgraph(deps))
    g.add_node("SQLAnalysisAndStrategyCompile", sql_strategy_subgraph(deps))
    g.add_node("HoconGenerationSubgraph", hocon_subgraph(deps))
    g.add_node("DeterministicGateSubgraph", gate_subgraph(deps))
    g.add_node("RepairSubgraph", repair_subgraph(deps))
    g.add_node("finalize", finalize)
    g.add_node("exhausted", exhausted)
    g.add_edge(START, "ParseIntentSubgraph")
    g.add_edge("ParseIntentSubgraph", "MetadataInspectionSubgraph")
    g.add_edge("MetadataInspectionSubgraph", "BuildEtlPlanSubgraph")
    g.add_edge("BuildEtlPlanSubgraph", "SQLAnalysisAndStrategyCompile")
    g.add_edge("SQLAnalysisAndStrategyCompile", "HoconGenerationSubgraph")
    g.add_edge("HoconGenerationSubgraph", "DeterministicGateSubgraph")
    g.add_conditional_edges(
        "DeterministicGateSubgraph", after_gate, {"finalize": "finalize", "repair": "RepairSubgraph", "exhausted": "exhausted"}
    )
    g.add_edge("RepairSubgraph", "SQLAnalysisAndStrategyCompile")
    g.add_edge("finalize", END)
    g.add_edge("exhausted", END)
    return g.compile(checkpointer=checkpointer)


def _sql_hash(sql: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(sql.encode("utf-8")).hexdigest()
