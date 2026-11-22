"""0001_initial_schema：创建 contracts/db/erd.md 定义的全部六大领域 71 张核心业务表。

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-11-22 22:25:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.shared.models import Base

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """基于冻结的 SQLAlchemy 2.0 ORM 元数据创建全部 6 大领域表、主键、外键、唯一约束与复合租户索引。"""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    """按依赖逆序卸载全部 6 大领域表。"""
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
