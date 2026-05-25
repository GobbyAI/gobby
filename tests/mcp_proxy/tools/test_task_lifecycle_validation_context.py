from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_validation import gather_validation_context

pytestmark = pytest.mark.unit


def test_gather_validation_context_prefers_linked_commit_diff_over_prose_summary() -> None:
    task = SimpleNamespace(
        id="task-1",
        commits=["abc123"],
        title="Fix validation evidence",
        validation_criteria="Diff is present",
        description=None,
    )
    diff_result = SimpleNamespace(
        diff="diff --git a/a.py b/a.py\n+change",
        commits=["abc123"],
        file_count=1,
    )

    with (
        patch(
            "gobby.tasks.commits.get_task_diff",
            return_value=diff_result,
        ) as get_task_diff,
        patch(
            "gobby.tasks.commits.summarize_diff_for_validation",
            return_value="summarized diff",
        ),
    ):
        context, raw_diff = gather_validation_context(
            task=task,
            changes_summary="prose only",
            repo_path="/repo",
            task_manager=MagicMock(),
        )

    get_task_diff.assert_called_once()
    assert raw_diff == diff_result.diff
    assert "Commit-based diff" in context
    assert "summarized diff" in context
    assert "Agent changes summary:\nprose only" in context
