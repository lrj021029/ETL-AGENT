"""通用分页参数与响应封装辅助模块。"""
from __future__ import annotations

from typing import Generic, Sequence, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageMeta(BaseModel):
    """契约标准分页元数据（对齐 contracts/openapi/openapi.yaml#/components/schemas/PageMeta）。"""

    total: int = Field(..., ge=0, description="符合条件的总记录数")
    page: int = Field(default=1, ge=1, description="当前页码（从 1 开始）")
    page_size: int = Field(default=20, ge=1, le=200, description="每页大小")


class PaginatedResponse(BaseModel, Generic[T]):
    """带分页元数据的通用列表响应结构。"""

    items: list[T]
    meta: PageMeta


def paginate_sequence(items: Sequence[T], page: int = 1, page_size: int = 20) -> PaginatedResponse[T]:
    """对内存或查询结果序列进行安全分页切片。"""
    safe_page = max(1, page)
    safe_size = max(1, min(page_size, 200))
    total = len(items)
    start = (safe_page - 1) * safe_size
    end = start + safe_size
    return PaginatedResponse(
        items=list(items[start:end]),
        meta=PageMeta(total=total, page=safe_page, page_size=safe_size),
    )
