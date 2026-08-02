"""
Reusable pagination primitives.

Design decision: `PaginationParams` is a single dependency-injectable
class every list/search endpoint takes as a query-param bundle (via
`Depends()`), and `Page[T]` is a single generic response wrapper every
list/search endpoint returns. This means adding pagination to a new
endpoint later is "accept `PaginationParams`, return `Page[YourSchema]`"
— no bespoke pagination math duplicated per-endpoint.

Offset-based (page/page_size) pagination is used rather than cursor-based,
which is simpler to reason about and sufficient for folder/file listings
that aren't high-frequency-write, high-page-depth feeds.
"""

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

MAX_PAGE_SIZE = 100


class PaginationParams:
    """FastAPI dependency: parses & validates `page`/`page_size` query params."""

    def __init__(
        self,
        page: int = Query(default=1, ge=1, description="1-indexed page number"),
        page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE, description="Items per page (max 100)"),
    ):
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class Page(BaseModel, Generic[T]):
    """Standard paginated response envelope, nested inside APIResponse.data."""

    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool

    @classmethod
    def create(cls, items: list[T], total: int, page: int, page_size: int) -> "Page[T]":
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return cls(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        )