"""多租户双重成员资格校验（Org+Project Dual Membership）、固定系统角色不可变保护与职责分离（SoD）服务。

核心安全不变量（遵循 PRD §3.1~3.3 与 contracts/openapi/error_codes.yaml）：
1. 双重成员强制校验：访问任何 Project 资源必须同时拥有 ACTIVE 的 `OrganizationMember` 与 ACTIVE 的 `ProjectMember`。
   一旦从 `OrganizationMember` 移除或禁用，即使 `ProjectMember` 仍存在，立即拒绝访问（`TENANT_ACCESS_DENIED`）。
2. 固定系统角色不可变：`MAKER`、`DATA_CHECKER`、`SECURITY_CHECKER`、`OPERATOR`、`AUDITOR`、`ORG_ADMIN`、`PROJECT_ADMIN`
   均为 `is_system=True`，严禁修改、删除或创建同名自定义角色覆盖（`SYSTEM_ROLE_IMMUTABLE` 409）。
3. 自定义角色不得绕过职责分离（SoD）：禁止自定义角色声明 `sod_bypass`、`self_approve` 或同时绑定
   Maker 申请提交权限与全审批槽位自批权限（`SOD_VIOLATION` 403）。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.service import SYSTEM_ROLE_CODES
from app.shared.errors import AppError, ErrorCode
from app.shared.models.iam import (
    MemberRoleBinding,
    OrganizationMember,
    Permission,
    Project,
    ProjectMember,
    Role,
    RolePermission,
    User,
)

# 严禁自定义角色包含的越权权限标识
FORBIDDEN_SOD_PERMISSIONS: frozenset[str] = frozenset(
    {
        "sod:bypass",
        "approval:self_approve",
        "harness:bypass_broker",
        "capability:self_issue",
        "rls:bypass",
    }
)


class MembershipService:
    """负责双重成员资格核验、项目成员角色绑定、固定/自定义角色管理及 SoD 硬约束检查。"""

    def verify_dual_membership(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[OrganizationMember, ProjectMember]:
        """强制验证用户在目标 Organization 与目标 Project 同时具备 ACTIVE 成员关系。"""
        proj = session.get(Project, project_id)
        if proj is None or proj.organization_id != organization_id or proj.status != "ACTIVE":
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "目标项目不存在、已停用或不属于当前组织上下文",
                detail={"organization_id": str(organization_id), "project_id": str(project_id)},
            )

        org_member = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if org_member is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "用户不具备当前组织的活跃成员资格（OrganizationMember 校验失败）",
                detail={"organization_id": str(organization_id), "user_id": str(user_id)},
            )

        proj_member = session.execute(
            select(ProjectMember).where(
                ProjectMember.organization_id == organization_id,
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if proj_member is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "用户不具备当前项目的活跃成员资格（ProjectMember 校验失败）",
                detail={"project_id": str(project_id), "user_id": str(user_id)},
            )

        return org_member, proj_member

    def get_user_roles_and_permissions(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """校验双重成员资格后，获取用户在当前项目下的所有角色代码与权限集合。"""
        org_member, _ = self.verify_dual_membership(
            session,
            organization_id=organization_id,
            project_id=project_id,
            user_id=user_id,
        )

        bindings = (
            session.execute(
                select(MemberRoleBinding, Role)
                .join(Role, Role.id == MemberRoleBinding.role_id)
                .where(
                    MemberRoleBinding.organization_id == organization_id,
                    MemberRoleBinding.user_id == user_id,
                    or_(
                        MemberRoleBinding.project_id == project_id,
                        MemberRoleBinding.project_id.is_(None),
                    ),
                )
            )
            .all()
        )
        role_codes: list[str] = []
        role_ids: list[uuid.UUID] = []
        for _, role in bindings:
            if role.code not in role_codes:
                role_codes.append(role.code)
            role_ids.append(role.id)

        permissions: set[str] = set()
        if role_ids:
            perm_rows = (
                session.execute(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id.in_(role_ids))
                )
                .scalars()
                .all()
            )
            permissions.update(perm_rows)

        return {
            "user_id": str(user_id),
            "org_role": org_member.org_role,
            "project_roles": role_codes,
            "permissions": sorted(permissions),
        }

    def require_permission(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        permission_code: str,
    ) -> dict[str, Any]:
        """校验用户双重成员资格及指定细粒度权限。"""
        subject = self.get_user_roles_and_permissions(
            session,
            organization_id=organization_id,
            project_id=project_id,
            user_id=user_id,
        )
        if permission_code not in subject["permissions"] and "PROJECT_ADMIN" not in subject["project_roles"]:
            raise AppError(
                ErrorCode.PERMISSION_DENIED,
                f"当前用户缺少执行该操作所需的权限 '{permission_code}'",
                detail={"required_permission": permission_code, "user_roles": subject["project_roles"]},
            )
        return subject

    def list_project_members(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出项目内所有活跃成员及其绑定的角色。"""
        self.verify_dual_membership(
            session,
            organization_id=organization_id,
            project_id=project_id,
            user_id=actor_user_id,
        )

        rows = (
            session.execute(
                select(ProjectMember, User)
                .join(User, User.id == ProjectMember.user_id)
                .where(
                    ProjectMember.organization_id == organization_id,
                    ProjectMember.project_id == project_id,
                )
                .order_by(ProjectMember.joined_at.asc())
            )
            .all()
        )
        result: list[dict[str, Any]] = []
        for pm, user in rows:
            role_rows = (
                session.execute(
                    select(Role.code)
                    .join(MemberRoleBinding, MemberRoleBinding.role_id == Role.id)
                    .where(
                        MemberRoleBinding.organization_id == organization_id,
                        MemberRoleBinding.project_id == project_id,
                        MemberRoleBinding.user_id == user.id,
                    )
                )
                .scalars()
                .all()
            )
            result.append(
                {
                    "id": str(pm.id),
                    "organization_id": str(pm.organization_id),
                    "project_id": str(pm.project_id),
                    "user_id": str(user.id),
                    "email": user.email,
                    "display_name": user.display_name,
                    "status": pm.status,
                    "roles": list(role_rows),
                    "joined_at": pm.joined_at.isoformat(),
                }
            )
        return result

    def add_project_member_with_role(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        target_user_id: uuid.UUID,
        role_code: str,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """添加项目成员并绑定角色（目标用户必须已经是该 Organization 的活跃成员）。"""
        self.verify_dual_membership(
            session,
            organization_id=organization_id,
            project_id=project_id,
            user_id=actor_user_id,
        )

        target_org_member = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == target_user_id,
                OrganizationMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if target_org_member is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "目标用户尚未加入当前组织（OrganizationMember 缺失），禁止直接加入项目",
            )

        clean_role_code = role_code.strip().upper()
        role = session.execute(
            select(Role).where(
                Role.code == clean_role_code,
                or_(
                    Role.is_system.is_(True),
                    Role.project_id == project_id,
                ),
            )
        ).scalar_one_or_none()
        if role is None:
            raise AppError(
                ErrorCode.PERMISSION_DENIED,
                f"角色 '{clean_role_code}' 不存在",
            )

        pm = session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user_id,
            )
        ).scalar_one_or_none()
        if pm is None:
            pm = ProjectMember(
                organization_id=organization_id,
                project_id=project_id,
                user_id=target_user_id,
                status="ACTIVE",
            )
            session.add(pm)
            session.flush()
        else:
            pm.status = "ACTIVE"

        existing_binding = session.execute(
            select(MemberRoleBinding).where(
                MemberRoleBinding.organization_id == organization_id,
                MemberRoleBinding.project_id == project_id,
                MemberRoleBinding.user_id == target_user_id,
                MemberRoleBinding.role_id == role.id,
            )
        ).scalar_one_or_none()
        if existing_binding is None:
            session.add(
                MemberRoleBinding(
                    organization_id=organization_id,
                    project_id=project_id,
                    user_id=target_user_id,
                    role_id=role.id,
                    granted_by=actor_user_id,
                )
            )
            session.flush()

        return {
            "project_member_id": str(pm.id),
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "user_id": str(target_user_id),
            "role_code": role.code,
            "status": pm.status,
        }

    def list_project_roles(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出所有固定系统角色与当前项目的自定义角色。"""
        self.verify_dual_membership(
            session,
            organization_id=organization_id,
            project_id=project_id,
            user_id=actor_user_id,
        )

        roles = (
            session.execute(
                select(Role)
                .where(
                    or_(
                        Role.is_system.is_(True),
                        Role.project_id == project_id,
                    )
                )
                .order_by(Role.is_system.desc(), Role.code.asc())
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(r.id),
                "code": r.code,
                "name": r.name,
                "is_system": r.is_system,
                "description": r.description,
                "organization_id": str(r.organization_id) if r.organization_id else None,
                "project_id": str(r.project_id) if r.project_id else None,
            }
            for r in roles
        ]

    def create_custom_project_role(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        code: str,
        name: str,
        description: str = "",
        permission_codes: list[str] | None = None,
        override_sod: bool = False,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """创建项目级自定义角色。

        硬性安全约束：
        1. 严禁与固定系统角色同名或覆盖系统角色（抛出 `SYSTEM_ROLE_IMMUTABLE` 409）。
        2. 严禁设置 `override_sod=True`、授予 `FORBIDDEN_SOD_PERMISSIONS`、
           或在同一自定义角色中同时组合 Maker 提交（`pipeline:prepare`）与双审批槽位（`approval:data_check` + `approval:security_check`）
           企图绕过职责分离（抛出 `SOD_VIOLATION` 403）。
        """
        self.verify_dual_membership(
            session,
            organization_id=organization_id,
            project_id=project_id,
            user_id=actor_user_id,
        )

        clean_code = code.strip().upper()
        if clean_code in SYSTEM_ROLE_CODES:
            raise AppError(
                ErrorCode.SYSTEM_ROLE_IMMUTABLE,
                f"'{clean_code}' 是不可变固定系统角色，严禁通过自定义角色覆盖或修改",
                detail={"role_code": clean_code},
            )

        requested_perms = set(permission_codes or [])
        if override_sod or (requested_perms & FORBIDDEN_SOD_PERMISSIONS):
            raise AppError(
                ErrorCode.SOD_VIOLATION,
                "自定义角色严禁绕过系统职责分离（SoD）或申请自批特权",
                detail={"forbidden_permissions": sorted(requested_perms & FORBIDDEN_SOD_PERMISSIONS)},
            )

        # 检查是否在同一自定义角色里同时包含 Maker Prepare 与审批决策企图自批或独占双槽
        if "pipeline:prepare" in requested_perms and (
            "approval:data_check" in requested_perms or "approval:security_check" in requested_perms
        ):
            raise AppError(
                ErrorCode.SOD_VIOLATION,
                "自定义角色不可同时组合 Maker 发起 Prepare 权限与 Checker 审批权限（违反 Maker-Checker 职责分离）",
                detail={"requested_permissions": sorted(requested_perms)},
            )
        if "approval:data_check" in requested_perms and "approval:security_check" in requested_perms:
            raise AppError(
                ErrorCode.SOD_VIOLATION,
                "自定义角色不可同时持有 DATA_CHECKER 与 SECURITY_CHECKER 双独立审批槽位权限",
                detail={"requested_permissions": sorted(requested_perms)},
            )

        role = Role(
            organization_id=organization_id,
            project_id=project_id,
            code=clean_code,
            name=name.strip(),
            is_system=False,
            description=description,
        )
        session.add(role)
        session.flush()

        if requested_perms:
            perms = (
                session.execute(select(Permission).where(Permission.code.in_(requested_perms)))
                .scalars()
                .all()
            )
            for p in perms:
                session.add(RolePermission(role_id=role.id, permission_id=p.id))
            session.flush()

        return {
            "id": str(role.id),
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "code": role.code,
            "name": role.name,
            "is_system": role.is_system,
            "description": role.description,
            "permissions": sorted(requested_perms),
        }

    def update_or_delete_role(
        self,
        session: Session,
        *,
        role_id: uuid.UUID,
        action: str = "DELETE",
    ) -> None:
        """修改或删除角色时，若目标为固定系统角色（is_system=True），立即拒绝并抛出 SYSTEM_ROLE_IMMUTABLE。"""
        role = session.get(Role, role_id)
        if role is None:
            raise AppError(ErrorCode.PERMISSION_DENIED, "目标角色不存在")
        if role.is_system or role.code in SYSTEM_ROLE_CODES:
            raise AppError(
                ErrorCode.SYSTEM_ROLE_IMMUTABLE,
                f"固定系统角色 '{role.code}' 不可修改或删除",
                detail={"role_id": str(role.id), "role_code": role.code, "attempted_action": action},
            )
        if action == "DELETE":
            session.delete(role)
            session.flush()

    @staticmethod
    def enforce_approval_sod(
        *,
        maker_user_id: uuid.UUID,
        approver_user_id: uuid.UUID,
        target_slot_role: str,
        existing_slot_approvers: dict[str, uuid.UUID],
    ) -> None:
        """在审批阶段强制执行 Maker-Checker 与双槽职责分离硬校验。"""
        if maker_user_id == approver_user_id:
            raise AppError(
                ErrorCode.APPROVAL_SELF_APPROVE_FORBIDDEN,
                "职责分离（SoD）拦截：Maker 严禁审批自己发起的 Preparation 申请",
                detail={"maker_user_id": str(maker_user_id), "approver_user_id": str(approver_user_id)},
            )

        for slot_role, other_approver_id in existing_slot_approvers.items():
            if slot_role != target_slot_role and other_approver_id == approver_user_id:
                raise AppError(
                    ErrorCode.APPROVAL_SLOT_CONFLICT,
                    f"职责分离（SoD）拦截：同一用户不可同时担任 '{slot_role}' 与 '{target_slot_role}' 审批人",
                    detail={
                        "approver_user_id": str(approver_user_id),
                        "conflicting_slot": slot_role,
                        "target_slot": target_slot_role,
                    },
                )
