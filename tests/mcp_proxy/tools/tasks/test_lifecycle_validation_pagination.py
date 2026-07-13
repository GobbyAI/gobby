"""Large parent-tree coverage for lifecycle completion validation."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from gobby.mcp_proxy.tools.tasks._lifecycle_validation import validate_parent_task


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

    def list_tasks(*, limit: int, offset: int, **_filters):
        return children[offset : offset + limit]

    task_manager.list_tasks.side_effect = list_tasks
    ctx = SimpleNamespace(task_manager=task_manager)

    result = validate_parent_task(ctx, "parent")

    assert result.can_close is False
    assert result.extra == {"open_children": ["child-1000"]}
    assert [call.kwargs["offset"] for call in task_manager.list_tasks.call_args_list] == [
        0,
        200,
        400,
        600,
        800,
        1000,
    ]
