"""LLM Provider 抽象、OpenAI 兼容 Provider、确定性 Mock Provider 与外送网关。

* 所有外部调用必须经 ProviderGateway：先脱敏最小化，再调用，再记录审计元数据；
* 审计只存脱敏载荷摘要与元数据，绝不存储隐藏思维链（CoT）；
* DeterministicMockProvider 不是真实 LLM，元数据恒带 is_mock_llm=True。
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any, Callable, Protocol

from ..artifacts.canonical import canonical_bytes
from .sanitizer import sanitize_for_llm

# Provider 返回中若出现这些键，网关一律丢弃（不保存、不透传隐藏推理）
_COT_KEYS = {"reasoning", "chain_of_thought", "cot", "thinking", "scratchpad", "reasoning_content"}


class LLMProvider(Protocol):
    info: dict[str, Any]

    def parse_intent(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def build_plan(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def repair_plan(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class DeterministicMockProvider:
    """可重复的测试 / 基准 Provider。**不是真实 LLM 调用。**"""

    info = {"provider": "DeterministicMockProvider", "model": "deterministic-mock-v1", "is_mock_llm": True}

    def __init__(self, plan_fault: str | None = None, repair_mode: str = "fix"):
        self.plan_fault = plan_fault  # None | "bad_table"
        self.repair_mode = repair_mode  # "fix" | "noop"
        self.calls: list[tuple[str, dict[str, Any]]] = []  # 记录收到的（已脱敏）载荷，便于测试断言

    def parse_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("parse_intent", payload))
        return dict(payload.get("hints", {}))

    def _parts(self, payload: dict[str, Any], fault: str | None) -> dict[str, Any]:
        intent = payload["intent"]
        tables = payload["metadata"]["tables"]
        meta = tables[intent["source_table"]]
        cols = intent.get("columns") or list(meta["columns"])
        pks = set(intent.get("primary_keys") or [])
        sensitive = set(meta.get("sensitive_columns", []))
        mappings = [
            {
                "source_field": c,
                "target_field": c,
                "target_type": meta["columns"][c],
                "nullable": c not in pks,
                "masking_policy": "HASH_SHA256" if c in sensitive else "NONE",
            }
            for c in cols
        ]
        if fault == "bad_table":
            sql = "SELECT id FROM unknown_schema.hidden_table"
        else:
            sql = intent.get("sql") or f"SELECT {', '.join(cols)} FROM {intent['source_table']}"
        return {
            "plan_name": f"sync_{intent['source_table'].replace('.', '_')}",
            "field_mappings": mappings,
            "transformations": [
                {"step_id": "s1", "kind": "SELECT_SQL", "sql_dialect": payload["dialect"], "sql_text": sql}
            ],
        }

    def build_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("build_plan", payload))
        return self._parts(payload, self.plan_fault)

    def repair_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("repair_plan", payload))
        if self.repair_mode == "noop":
            return payload["plan_parts"]
        return self._parts(payload["context"], None)


# 各子图任务对应的严格 JSON 输出契约说明（禁止返回思维链字段，仅输出结构化 JSON 对象）
_TASK_SYSTEM_PROMPTS: dict[str, str] = {
    "parse_intent": (
        "You are an ETL intent parser. Reply with a single JSON object only (no markdown fences, no prose). "
        "Allowed keys: source_table (string like 'schema.table'), sink_table (string like 'schema.table'), "
        "sync_mode ('FULL_LOAD' | 'INCREMENTAL' | 'CDC'), write_mode ('SHADOW_SWAP' | 'UPSERT' | 'APPEND'), "
        "incremental_column (string or null), primary_keys (array of strings), columns (array of strings or null). "
        "If the prompt mentions 增量/incremental, set sync_mode='INCREMENTAL'. "
        "If the prompt mentions CDC/实时变更, set sync_mode='CDC'. "
        "Merge any provided hints unless the user prompt explicitly specifies a more precise value."
    ),
    "build_plan_parts": (
        "You are an ETL plan compiler. Reply with a single JSON object only (no markdown fences, no prose). "
        "Required keys: "
        "1) plan_name (string, <=64 chars, letters/digits/underscores); "
        "2) field_mappings (array of objects with keys: source_field, target_field, target_type, nullable (bool), "
        "masking_policy ('NONE' | 'HASH_SHA256' | 'MASK_PARTIAL' | 'REDACT')); "
        "3) transformations (array with 1 object: {step_id: 's1', kind: 'SELECT_SQL', sql_dialect: '<dialect>', "
        "sql_text: 'SELECT <cols> FROM <source_table>'}). "
        "Use ONLY tables and columns present in metadata.tables. Apply HASH_SHA256 or MASK_PARTIAL to sensitive columns."
    ),
    "repair_plan_parts": (
        "You are an ETL plan repairer. Fix all deterministic gate failures in plan_parts using only tables/columns in context.metadata.tables. "
        "Reply with a single JSON object with keys: plan_name, field_mappings, transformations (same schema as build_plan_parts)."
    ),
}


def _extract_json_object(raw_text: Any) -> dict[str, Any] | None:
    """从 LLM 返回文本中稳健提取首个 JSON 对象（兼容 markdown 代码块与 None/空字符串）。"""
    if not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _normalize_parse_intent_output(parsed: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    """将真实 LLM 输出的多种合理结构归一化为 ParseIntentSubgraph 所需的标准扁平字典。"""
    import re as _re

    out: dict[str, Any] = dict(payload.get("hints") or {})
    if isinstance(parsed, dict):
        # 兼容嵌套格式：{"source": {"database": "db", "table": "t"}, "target": {"database": "d", "table": "t2"}}
        src = parsed.get("source")
        if isinstance(src, dict) and src.get("table") and not parsed.get("source_table"):
            db = src.get("database") or src.get("schema")
            parsed["source_table"] = f"{db}.{src['table']}" if db else str(src["table"])
        tgt = parsed.get("target") or parsed.get("sink")
        if isinstance(tgt, dict) and tgt.get("table") and not parsed.get("sink_table"):
            db = tgt.get("database") or tgt.get("schema")
            parsed["sink_table"] = f"{db}.{tgt['table']}" if db else str(tgt["table"])

        for key in ("source_table", "sink_table", "sync_mode", "write_mode", "incremental_column", "primary_keys", "columns", "sql"):
            val = parsed.get(key)
            if val not in (None, "", []):
                out[key] = val

    # 结合自然语言 Prompt 补充识别缺失字段，防止外部免费模型偶发漏字段或返回空内容
    prompt = str(payload.get("prompt") or "")
    if not out.get("sync_mode") or (out.get("sync_mode") == "FULL_LOAD" and ("增量" in prompt or "incremental" in prompt.lower())):
        if "cdc" in prompt.lower() or "实时变更" in prompt:
            out["sync_mode"] = "CDC"
        elif "增量" in prompt or "incremental" in prompt.lower():
            out["sync_mode"] = "INCREMENTAL"
        elif "全量" in prompt or "full" in prompt.lower():
            out["sync_mode"] = "FULL_LOAD"

    if out.get("sync_mode"):
        sm = str(out["sync_mode"]).upper()
        if sm in ("FULL", "BATCH"):
            sm = "FULL_LOAD"
        out["sync_mode"] = sm

    if not out.get("source_table"):
        m_src = _re.search(r"([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)", prompt)
        if m_src:
            out["source_table"] = m_src.group(1)

    if not out.get("sink_table"):
        m_tgt = _re.search(r"(?:至|到|to)\s*(?:Doris|doris|MySQL|mysql|Postgres|postgres)?\s*([a-zA-Z_][a-zA-Z0-9_.]*)", prompt)
        if m_tgt:
            out["sink_table"] = m_tgt.group(1).strip(".")

    return out


class OpenAICompatibleProvider:
    """OpenAI 兼容 /chat/completions Provider；传输层可注入，便于离线测试。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: Callable[[str, dict[str, str], bytes], bytes] | None = None,
        timeout: float = 60.0,
    ):
        self._base = base_url.rstrip("/")
        self._key = api_key  # 仅用于 Authorization 头，不进入日志 / 审计
        self._timeout = timeout
        self._transport = transport or self._urllib_transport
        self.info = {"provider": "OpenAICompatibleProvider", "model": model, "is_mock_llm": False}

    def _urllib_transport(self, url: str, headers: dict[str, str], body: bytes) -> bytes:
        import time as _time
        import urllib.error as _uerr

        # 附加标准客户端 User-Agent 与 Accept 头，并对上游 429/502/503/504 执行有限指数退避重试
        req_headers = {
            "Accept": "application/json",
            "User-Agent": "etl-agent/1.0 (OpenAI-Compatible)",
            **headers,
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as r:  # noqa: S310
                    return r.read()
            except _uerr.HTTPError as exc:
                last_exc = exc
                if exc.code in (429, 502, 503, 504) and attempt < 2:
                    _time.sleep(0.8 * (2**attempt))
                    continue
                raise
            except (_uerr.URLError, TimeoutError) as exc:
                last_exc = exc
                if attempt < 2:
                    _time.sleep(0.8 * (2**attempt))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _chat(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        sys_prompt = _TASK_SYSTEM_PROMPTS.get(task, f"Task: {task}. Reply with a single JSON object only.")
        body = {
            "model": self.info["model"],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            ],
        }
        raw = self._transport(
            f"{self._base}/chat/completions",
            {"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
            json.dumps(body).encode("utf-8"),
        )
        envelope = json.loads(raw)
        choices = envelope.get("choices") or []
        msg = choices[0].get("message", {}) if choices else {}
        parsed = _extract_json_object(msg.get("content"))
        if parsed is not None:
            return parsed
        return {}

    def _fallback_plan_parts(self, payload: dict[str, Any]) -> dict[str, Any]:
        """当外部 LLM 返回不完整字段或空内容时，依据只读元数据快照确定性补齐合法 Plan 部件。"""
        ctx = payload.get("context", payload)
        intent = ctx.get("intent") or {}
        tables = (ctx.get("metadata") or {}).get("tables") or {}
        src_table = str(intent.get("source_table") or "")
        meta = tables.get(src_table)
        if meta is None and tables:
            bare = src_table.split(".")[-1].lower()
            matched_key = next((k for k in tables if k.split(".")[-1].lower() == bare), next(iter(tables)))
            src_table = matched_key
            meta = tables[matched_key]
        cols = list(intent.get("columns") or (meta.get("columns", {}).keys() if meta else ["id"]))
        pks = set(intent.get("primary_keys") or (meta.get("primary_keys", []) if meta else []))
        sensitive = set(meta.get("sensitive_columns", []) if meta else [])
        col_types = (meta.get("columns") or {}) if meta else {}
        mappings = [
            {
                "source_field": c,
                "target_field": c,
                "target_type": col_types.get(c, "VARCHAR(255)"),
                "nullable": c not in pks,
                "masking_policy": "HASH_SHA256" if c in sensitive else "NONE",
            }
            for c in cols
        ]
        sql = intent.get("sql") or f"SELECT {', '.join(cols)} FROM {src_table}"
        return {
            "plan_name": f"sync_{src_table.replace('.', '_')}"[:64],
            "field_mappings": mappings,
            "transformations": [
                {
                    "step_id": "s1",
                    "kind": "SELECT_SQL",
                    "sql_dialect": ctx.get("dialect", "mysql"),
                    "sql_text": sql,
                }
            ],
        }

    def parse_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        parsed = self._chat("parse_intent", payload)
        return _normalize_parse_intent_output(parsed, payload)

    def build_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        parsed = self._chat("build_plan_parts", payload)
        fallback = self._fallback_plan_parts(payload)
        if not parsed.get("field_mappings") or not parsed.get("transformations"):
            return {**fallback, **{k: v for k, v in parsed.items() if v}}
        return parsed

    def repair_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        parsed = self._chat("repair_plan_parts", payload)
        fallback = self._fallback_plan_parts(payload)
        if not parsed.get("field_mappings") or not parsed.get("transformations"):
            return fallback
        return parsed


class ProviderGateway:
    """唯一的 LLM 外送出口：脱敏 -> 调用 -> 审计。"""

    def __init__(self, provider: LLMProvider, approved_row_access: bool = False):
        self.provider = provider
        self._approved_rows = approved_row_access

    def call(self, method: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        clean, report = sanitize_for_llm(payload, approved_row_access=self._approved_rows)
        result = getattr(self.provider, method)(clean)
        result = {k: v for k, v in result.items() if k.lower() not in _COT_KEYS}
        audit = {
            "task": method,
            "provider": self.provider.info["provider"],
            "model": self.provider.info["model"],
            "is_mock_llm": bool(self.provider.info["is_mock_llm"]),
            "payload_sha256": "sha256:" + hashlib.sha256(canonical_bytes(clean)).hexdigest(),
            "redaction": report.as_dict(),
            "hidden_cot_stored": False,
        }
        return result, audit
