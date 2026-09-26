"""Pagination: ``PageParams`` as a dependency, ``Page[Schema]`` as the response model.

::

    @router.get("/posts", response_model=Page[PostRead])
    async def list_posts(params: PageParams = Depends()):
        return await Post.objects.order_by("-id").paginate(params)
"""

from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, computed_field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)

    items: list[T]
    total: int
    page: int
    size: int
    pages: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_previous(self) -> bool:
        return self.page > 1


class PageParams:
    """``?page=2&size=50`` query parameters, validated (size is capped at ``max_size``)."""

    max_size = 100

    def __init__(
        self,
        page: int = Query(1, ge=1, description="Page number, starting at 1"),
        size: int = Query(20, ge=1, le=100, description="Items per page"),
    ) -> None:
        self.page = page
        self.size = min(size, self.max_size)
