"""Agent 上下文、租户错误与线程归属 / 权限校验接口。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


class TenantAccessDenied(Exception):
    """用户 / 组织 / 项目与线程归属不符，或权限已被撤销。"""

    code = "TENANT_ACCESS_DENIED"


@dataclass(frozen=True)
class AgentContext:
    user_id: str
    organization_id: str
    project_id: str
    thread_id: str

    def owner(self) -> dict[str, str]:
        d = asdict(self)
        return d


class PermissionChecker(Protocol):
    """生产环境由 Security/PDP 提供实现；此处仅定义最小接口。"""

    def can_access(self, ctx: AgentContext) -> bool: ...


class AllowAllChecker:
    """仅用于单元测试。"""

    def can_access(self, ctx: AgentContext) -> bool:  # noqa: ARG002
        return True


class ThreadOwnershipStore:
    """thread_id -> 创建者 (user, org, project)。生产环境应由数据库持久化实现。"""

    def __init__(self) -> None:
        self._owners: dict[str, tuple[str, str, str]] = {}

    def register(self, ctx: AgentContext) -> None:
        self._owners.setdefault(ctx.thread_id, (ctx.user_id, ctx.organization_id, ctx.project_id))

    def verify(self, ctx: AgentContext) -> bool:
        return self._owners.get(ctx.thread_id) == (ctx.user_id, ctx.organization_id, ctx.project_id)
