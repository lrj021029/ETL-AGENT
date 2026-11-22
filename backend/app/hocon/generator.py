"""确定性 SeaTunnel HOCON 生成器。

输入：已校验的 EtlPlan + Connector Capability Registry + 两阶段 QualityContract。
输出：仅含 ${SECRET:...} 占位符的 HOCON 文本，绝不输出明文密钥。
相同输入必得逐字节相同的输出（无时间戳、无随机数、键顺序固定）。
"""
from __future__ import annotations

import json
from typing import Any

from ..pipeline.models import EtlPlan, QualityContract

from .registry import ConnectorRegistry, check_plan_capabilities
from .validator import HoconIssue, HoconValidationError, enforce_hocon

# connector 家族 -> (SeaTunnel 插件名, 驱动)
_FAMILY = {
    "mysql": ("Jdbc", "com.mysql.cj.jdbc.Driver"),
    "postgres": ("Jdbc", "org.postgresql.Driver"),
    "doris": ("Doris", None),
}


def connector_family(connector_id: str) -> str:
    cid = connector_id.lower()
    for fam in ("mysql", "postgres", "doris"):
        if cid.startswith(fam):
            return fam
    if cid.startswith("pg"):
        return "postgres"
    return cid.split("-")[0].split("_")[0]


def _ph(prefix: str, name: str) -> str:
    return "${SECRET:%s_%s}" % (prefix, name)


def _q(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def _render(value: Any, indent: int) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        lines = ["{"]
        for k, v in value.items():
            if isinstance(v, dict):
                lines.append(f"{pad}  {k} {_render(v, indent + 1)}")
            else:
                lines.append(f"{pad}  {k} = {_render(v, indent + 1)}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        return "[" + ", ".join(_render(v, indent + 1) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if value is None:
        return "null"
    return _q(str(value))


def needs_quality_split(quality: QualityContract | None) -> bool:
    return bool(quality and any(r.stage == "STREAMING_ROW_SPLIT" for r in quality.rules))


def _source_block(plan: EtlPlan, family: str, query: str) -> dict[str, Any]:
    plugin, driver = _FAMILY[family]
    if plugin != "Jdbc":
        raise HoconValidationError([HoconIssue("HOCON_VALIDATION_FAILED", "source", f"unsupported source family {family}")])
    return {
        plugin: {
            "plugin_output": "src",
            "url": _ph("SRC", "URL"),
            "driver": driver,
            "user": _ph("SRC", "USER"),
            "password": _ph("SRC", "PWD"),
            "query": query,
        }
    }


def _sink_block(plan: EtlPlan, family: str, input_alias: str) -> dict[str, Any]:
    s = plan.sink
    target = f"{s.target_table}__shadow" if s.write_mode == "SHADOW_SWAP" else s.target_table
    if family == "doris":
        return {
            "Doris": {
                "plugin_input": input_alias,
                "fenodes": _ph("SINK", "FENODES"),
                "username": _ph("SINK", "USER"),
                "password": _ph("SINK", "PWD"),
                "database": s.target_schema,
                "table": target,
                "sink.enable-2pc": True,
                "sink.label-prefix": f"etl_{plan.plan_name}"[:64],
            }
        }
    plugin, driver = _FAMILY[family]
    return {
        plugin: {
            "plugin_input": input_alias,
            "url": _ph("SINK", "URL"),
            "driver": driver,
            "user": _ph("SINK", "USER"),
            "password": _ph("SINK", "PWD"),
            "table": f"{s.target_schema}.{target}",
            "generate_sink_sql": True,
        }
    }


def generate_hocon(plan: EtlPlan, registry: ConnectorRegistry, quality: QualityContract | None = None) -> str:
    """生成并结构校验 HOCON；能力不足时抛 HoconValidationError(CONNECTOR_CAPABILITY_UNSUPPORTED)。"""
    split = needs_quality_split(quality)
    cap_issues = check_plan_capabilities(
        registry,
        source_connector=plan.source.connector_id,
        sink_connector=plan.sink.connector_id,
        sync_mode=plan.sync_mode,
        write_mode=plan.sink.write_mode,
        needs_quality_split=split,
    )
    if cap_issues:
        raise HoconValidationError([HoconIssue(i.code, "capabilities", i.detail) for i in cap_issues])

    src_family = connector_family(plan.source.connector_id)
    sink_family = connector_family(plan.sink.connector_id)
    if src_family not in _FAMILY or sink_family not in _FAMILY:
        raise HoconValidationError([HoconIssue("CONNECTOR_CAPABILITY_UNSUPPORTED", "connector", "no HOCON template")])

    # 字段映射 -> 源端查询；SQL 步骤按已解析的执行策略落位
    obj = plan.source.objects[0]
    cols = ", ".join(m.source_field for m in plan.field_mappings if not m.transform_expr) or "*"
    base_query = f"SELECT {cols} FROM {obj.qualified}"
    pushdown = [t for t in plan.transformations if t.resolved_execution_strategy == "SOURCE_PUSHDOWN"]
    query = pushdown[0].sql_text if pushdown else base_query
    seatunnel_steps = [t for t in plan.transformations if t.resolved_execution_strategy == "SEATUNNEL_TRANSFORM"]

    env: dict[str, Any] = {
        "parallelism": 2,
        "job.mode": "STREAMING" if plan.sync_mode == "CDC" else "BATCH",
    }
    # 变换链：(插件名, 配置)；同名插件多次出现时输出为 HOCON 数组，避免被同名对象合并
    chain: list[tuple[str, dict[str, Any]]] = []
    prev = "src"
    for i, t in enumerate(seatunnel_steps):
        out = f"t{i}"
        chain.append(("Sql", {"plugin_input": prev, "plugin_output": out, "query": t.sql_text}))
        prev = out
    if split:
        out = "split_out"
        chain.append(
            (
                "DataQualitySplit",
                {
                    "plugin_input": prev,
                    "plugin_output": out,
                    "error_table": quality.error_table_name,  # type: ignore[union-attr]
                    "raw_value_storage_policy": quality.raw_value_storage_policy,  # type: ignore[union-attr]
                    "rules": [
                        {
                            "rule_id": r.rule_id,
                            "rule_type": r.rule_type,
                            "column": r.column_name or "",
                            "error_code": r.error_code,
                            "severity": r.severity,
                            "params": json.dumps(r.params, sort_keys=True, ensure_ascii=False),
                        }
                        for r in quality.rules  # type: ignore[union-attr]
                        if r.stage == "STREAMING_ROW_SPLIT"
                    ],
                },
            )
        )
        prev = out

    parts = [
        "# 由 ETL-Agent 确定性编译器生成；密钥仅为占位符，禁止手工填写明文",
        "env " + _render(env, 0),
        "source " + _render(_source_block(plan, src_family, query), 0),
    ]
    for t in plan.transformations:
        parts.append(f"# step {t.step_id}: {t.resolved_execution_strategy} reasons={','.join(t.strategy_reason_codes)}")
    if chain:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for name, cfg in chain:
            grouped.setdefault(name, []).append(cfg)
        tr_lines = ["transform {"]
        for name, cfgs in grouped.items():
            if len(cfgs) == 1:
                tr_lines.append(f"  {name} {_render(cfgs[0], 1)}")
            else:
                tr_lines.append(f"  {name} = [")
                tr_lines.append(",\n".join("    " + _render(c, 2) for c in cfgs))
                tr_lines.append("  ]")
        tr_lines.append("}")
        parts.append("\n".join(tr_lines))
    parts.append("sink " + _render(_sink_block(plan, sink_family, prev), 0))
    text = "\n".join(parts) + "\n"
    enforce_hocon(text)
    return text
