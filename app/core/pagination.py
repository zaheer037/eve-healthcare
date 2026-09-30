"""Offset pagination with hard caps (architecture §5.1).

`limit` is 1 to 100 and `offset` is 0 to 10,000, so the worst page is bounded. Every list has
a fixed order with an `id` tie-breaker. `# ponytail: offset pagination -> keyset on
(created_at, id) when a user can hold thousands of bookings`.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_OFFSET = 10_000


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int


class Page[T](BaseModel):
    data: list[T]
    pagination: Pagination


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def describe(self, total: int) -> Pagination:
        return Pagination(total=total, limit=self.limit, offset=self.offset)


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageParamsDep = Annotated[PageParams, Depends(page_params)]
