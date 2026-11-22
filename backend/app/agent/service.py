"""AgentService：对外入口，负责租户隔离、权限二次校验、中断 / 恢复。

恢复（resume）必须同时满足：
1. thread_id 的归属 (user_id, organization_id, project_id) 与调用者完全一致；
2. PermissionChecker 在恢复时刻重新放行（权限可能已被撤销）。
任一失败即 TENANT_ACCESS_DENIED，且不触碰 checkpoint。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from .context import AgentContext, PermissionChecker, TenantAccessDenied, ThreadOwnershipStore
from .graph import AgentDeps, build_agent_graph


def make_checkpointer(postgres_conn_string: str | None = None) -> Any:
    """生产使用 PostgreSQL Checkpointer；未提供连接串（单元测试）回退到内存实现。"""
    if not postgres_conn_string:
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("langgraph-checkpoint-postgres 未安装，无法启用 PostgreSQL Checkpointer") from e
    cm = PostgresSaver.from_conn_string(postgres_conn_string)  # pragma: no cover
    saver = cm.__enter__()  # pragma: no cover
    saver.setup()  # pragma: no cover
    return saver  # pragma: no cover


@dataclass
class AgentResult:
    status: str  # INTERRUPTED | CANDIDATE_READY | HANDED_BACK_TO_MAKER
    thread_id: str
    interrupt: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    handback: dict[str, Any] | None = None
    error_code: str | None = None


class AgentService:
    def __init__(
        self,
        deps: AgentDeps,
        permissions: PermissionChecker,
        ownership: ThreadOwnershipStore | None = None,
        checkpointer: Any = None,
    ):
        deps.tools.assert_read_only()  # 启动自检：工具边界
        self._permissions = permissions
        self._ownership = ownership or ThreadOwnershipStore()
        self._graph = build_agent_graph(deps, checkpointer or make_checkpointer())

    @staticmethod
    def _cfg(ctx: AgentContext) -> dict[str, Any]:
        return {"configurable": {"thread_id": ctx.thread_id}}

    def start(self, ctx: AgentContext, request: dict[str, Any]) -> AgentResult:
        if not self._permissions.can_access(ctx):
            raise TenantAccessDenied("permission denied")
        if self._ownership.verify(ctx) is False and ctx.thread_id in self._ownership._owners:  # noqa: SLF001
            raise TenantAccessDenied("thread belongs to another tenant")
        self._ownership.register(ctx)
        state = {"owner": ctx.owner(), "request": request, "repair_attempts": 0, "llm_audit": []}
        self._graph.invoke(state, self._cfg(ctx))
        return self._result(ctx)

    def resume(self, ctx: AgentContext, answers: dict[str, Any]) -> AgentResult:
        if not self._ownership.verify(ctx):
            raise TenantAccessDenied("thread not owned by caller")
        if not self._permissions.can_access(ctx):
            raise TenantAccessDenied("permission revoked")
        self._graph.invoke(Command(resume={"answers": answers, "ctx": ctx.owner()}), self._cfg(ctx))
        return self._result(ctx)

    def _result(self, ctx: AgentContext) -> AgentResult:
        snap = self._graph.get_state(self._cfg(ctx))
        if snap.next:
            payload = None
            for t in snap.tasks:
                for i in t.interrupts:
                    payload = i.value
            return AgentResult("INTERRUPTED", ctx.thread_id, interrupt=payload)
        v = snap.values
        return AgentResult(
            v["status"],
            ctx.thread_id,
            candidate=v.get("candidate"),
            handback=v.get("handback"),
            error_code=v.get("error_code"),
        )
