"""组织（Organization）与组织成员管理领域服务。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.shared.errors import AppError, ErrorCode
from app.shared.models.iam import Organization, OrganizationMember, User


class OrganizationService:
    """提供组织创建、当前用户所属组织查询与组织成员管理服务。"""

    def list_organizations_for_user(self, session: Session, *, user_id: uuid.UUID) -> list[dict[str, Any]]:
        """列出当前用户拥有 ACTIVE 组织成员资格的所有组织。"""
        rows = (
            session.execute(
                select(Organization, OrganizationMember)
                .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
                .where(
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.status == "ACTIVE",
                    Organization.status == "ACTIVE",
                )
                .order_by(Organization.created_at.asc())
            )
            .all()
        )
        return [
            {
                "id": str(org.id),
                "slug": org.slug,
                "name": org.name,
                "status": org.status,
                "org_role": member.org_role,
                "created_at": org.created_at.isoformat(),
            }
            for org, member in rows
        ]

    def create_organization(
        self,
        session: Session,
        *,
        slug: str,
        name: str,
        creator_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """创建新组织，并自动将创建者设为 ORG_ADMIN 组织成员。"""
        clean_slug = slug.strip().lower()
        existing = session.execute(select(Organization).where(Organization.slug == clean_slug)).scalar_one_or_none()
        if existing is not None:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"组织标识 slug '{clean_slug}' 已存在",
            )

        org = Organization(slug=clean_slug, name=name.strip(), status="ACTIVE")
        session.add(org)
        session.flush()

        member = OrganizationMember(
            organization_id=org.id,
            user_id=creator_user_id,
            org_role="ORG_ADMIN",
            status="ACTIVE",
        )
        session.add(member)
        session.flush()

        return {
            "id": str(org.id),
            "slug": org.slug,
            "name": org.name,
            "status": org.status,
            "org_role": member.org_role,
            "created_at": org.created_at.isoformat(),
        }

    def list_organization_members(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出指定组织内的全部成员（要求调用者本身为该组织活跃成员）。"""
        caller_membership = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == actor_user_id,
                OrganizationMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if caller_membership is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "当前用户不属于目标组织，禁止访问组织成员列表",
            )

        rows = (
            session.execute(
                select(OrganizationMember, User)
                .join(User, User.id == OrganizationMember.user_id)
                .where(OrganizationMember.organization_id == organization_id)
                .order_by(OrganizationMember.joined_at.asc())
            )
            .all()
        )
        return [
            {
                "id": str(om.id),
                "organization_id": str(om.organization_id),
                "user_id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "org_role": om.org_role,
                "status": om.status,
                "joined_at": om.joined_at.isoformat(),
            }
            for om, user in rows
        ]

    def add_organization_member(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        org_role: str = "MEMBER",
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """向组织添加或激活成员（要求调用者为该组织活跃成员）。"""
        caller = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == actor_user_id,
                OrganizationMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if caller is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "当前用户无权管理目标组织成员",
            )

        user = session.get(User, user_id)
        if user is None:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标用户不存在")

        existing = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = "ACTIVE"
            existing.org_role = org_role
            session.flush()
            member = existing
        else:
            member = OrganizationMember(
                organization_id=organization_id,
                user_id=user_id,
                org_role=org_role,
                status="ACTIVE",
            )
            session.add(member)
            session.flush()

        return {
            "id": str(member.id),
            "organization_id": str(member.organization_id),
            "user_id": str(member.user_id),
            "org_role": member.org_role,
            "status": member.status,
            "joined_at": member.joined_at.isoformat(),
        }

    def remove_organization_member(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """移除或禁用用户的组织成员资格（双重成员校验下，移除后立即丧失该组织下所有 Project 访问权）。"""
        existing = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = "REMOVED"
            session.flush()
