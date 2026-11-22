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
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self._timeout) as r:  # noqa: S310
            return r.read()

    def _chat(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.info["model"],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": f"Task: {task}. Reply with a single JSON object only."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            ],
        }
        raw = self._transport(
            f"{self._base}/chat/completions",
            {"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
            json.dumps(body).encode("utf-8"),
        )
        content = json.loads(raw)["choices"][0]["message"]["content"]
        return json.loads(content)

    def parse_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._chat("parse_intent", payload)

    def build_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._chat("build_plan_parts", payload)

    def repair_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._chat("repair_plan_parts", payload)


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
