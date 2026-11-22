"""Agent 只读工具注册表。

硬边界：Agent 不得拥有任何副作用工具——无数据库写入、无 SeaTunnel 提交、
无 Capability 签发、无 Commit、无 Rollback。注册时即做名称与声明双重校验，
并提供 assert_read_only() 供测试与启动自检使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

# 工具名中不得出现的副作用动词（按词元匹配）
FORBIDDEN_TOKENS = frozenset(
    {
        "write", "insert", "update", "delete", "drop", "truncate", "alter", "create", "submit",
        "execute", "run", "launch", "start", "cancel", "issue", "sign", "mint", "grant", "commit",
        "rollback", "publish", "approve", "swap", "apply", "deploy", "save", "persist", "send",
    }
)


class ToolBoundaryViolation(Exception):
    pass


@dataclass(frozen=True)
class ReadOnlyTool:
    name: str
    description: str
    fn: Callable[..., Any]
    side_effect: str = "READ_ONLY"


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


class ReadOnlyToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ReadOnlyTool] = {}

    def register(self, tool: ReadOnlyTool) -> None:
        if tool.side_effect != "READ_ONLY":
            raise ToolBoundaryViolation(f"{tool.name}: side_effect must be READ_ONLY")
        bad = _tokens(tool.name) & FORBIDDEN_TOKENS
        if bad:
            raise ToolBoundaryViolation(f"{tool.name}: forbidden side-effect verb(s) {sorted(bad)}")
        if tool.name in self._tools:
            raise ToolBoundaryViolation(f"{tool.name}: duplicate tool")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[ReadOnlyTool]:
        return [self._tools[n] for n in self.names()]

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in self._tools:
            raise ToolBoundaryViolation(f"unknown tool {name}")
        return self._tools[name].fn(*args, **kwargs)

    def assert_read_only(self) -> None:
        for t in self._tools.values():
            if t.side_effect != "READ_ONLY" or _tokens(t.name) & FORBIDDEN_TOKENS:
                raise ToolBoundaryViolation(t.name)


def build_default_registry(
    metadata_reader: Callable[..., Any], capability_lister: Callable[[], Any]
) -> ReadOnlyToolRegistry:
    """默认只读工具集：元数据快照读取、Connector 能力查询。"""
    reg = ReadOnlyToolRegistry()
    reg.register(ReadOnlyTool("inspect_metadata_snapshot", "读取不可变元数据快照（只读）", metadata_reader))
    reg.register(ReadOnlyTool("list_connector_capabilities", "查询 Connector 能力矩阵（只读）", capability_lister))
    return reg
