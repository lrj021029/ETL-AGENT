"""SQLAlchemy 2.0 声明式基类与通用字段类型定义。

所有业务表统一使用 UUID 主键与 TIMESTAMPTZ（UTC 时间戳），
租户表按组织级（organization_id）与项目级（organization_id, project_id）严格分类以便自动生成 RLS 策略。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """获取当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """全局 SQLAlchemy 2.0 声明式 ORM 基类。"""

    type_annotation_map = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
        datetime: DateTime(timezone=True),
        uuid.UUID: Uuid(as_uuid=True),
    }


class UUIDPrimaryKeyMixin:
    """统一 UUID 主键 Mixin。"""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
