"""Large parent-tree coverage for lifecycle completion validation."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from gobby.mcp_proxy.tools.tasks._close_context import children as collect_children
from gobby.mcp_proxy.tools.tasks._context import RegistryContext


def test_parent_close_finds_open_child_after_one_thousand_siblings() -> None:
    children = [
        SimpleNamespace(
            id=f"child-{index}",
            title=f"Child {index}",
            closed_at="now" if index < 1000 else None,
        )
        for index in range(1001)
    ]
    task_manager = MagicMock()

    def list_tasks(*, limit: int, offset: int, **_filters: object) -> list[SimpleNamespace]:
        return children[offset : offset + limit]

    task_manager.list_tasks.side_effect = list_tasks
    ctx = SimpleNamespace(task_manager=task_manager)

    result = collect_children(cast(RegistryContext, ctx), "parent")

    assert len(result) == 1001
    assert [child.id for child in result if child.closed_at is None] == ["child-1000"]
    assert [call.kwargs["offset"] for call in task_manager.list_tasks.call_args_list] == [
        0,
        200,
        400,
        600,
        800,
        1000,
    ]
