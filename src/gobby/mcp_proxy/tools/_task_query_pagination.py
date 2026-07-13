"""Helpers for exhausting paginated task-manager queries."""

from collections.abc import Callable
from typing import Any

TASK_QUERY_PAGE_SIZE = 200


def collect_task_query_pages[T](
    query: Callable[..., list[T]],
    /,
    *,
    page_size: int = TASK_QUERY_PAGE_SIZE,
    **filters: Any,
) -> list[T]:
    """Return every result from a task-manager query that supports limit/offset.

    A repeated full page means the query did not honor ``offset``. Failing with
    a diagnostic is safer than looping forever or silently returning a prefix.
    """
    if page_size <= 0:
        raise ValueError("Task query page_size must be positive")

    results: list[T] = []
    offset = 0
    full_page_identities: set[tuple[object, ...]] = set()

    while True:
        page = query(limit=page_size, offset=offset, **filters)
        if len(page) > page_size:
            raise RuntimeError(
                f"Task query returned {len(page)} rows for page_size={page_size}; "
                "the backend must honor limit/offset pagination"
            )

        results.extend(page)
        if len(page) < page_size:
            return results

        identity = tuple(getattr(item, "id", id(item)) for item in page)
        if identity in full_page_identities:
            raise RuntimeError(
                f"Task query pagination did not advance at offset={offset}; "
                "the backend must honor limit/offset pagination"
            )
        full_page_identities.add(identity)
        offset += len(page)
