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
        diff=(
            "diff --git a/a.py b/a.py\n"
            "index abc..def 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+change\n"
        ),
        commits=["abc123"],
        file_count=1,
    )
    long_summary = "prose only\n" + ("x" * 50000)

    with patch(
        "gobby.tasks.commits.get_task_diff",
        return_value=diff_result,
    ) as get_task_diff:
        context = gather_validation_context(
            task=task,
            changes_summary=long_summary,
            repo_path="/repo",
            task_manager=MagicMock(),
        )

    get_task_diff.assert_called_once()
    assert context.raw_diff == diff_result.diff
    assert context.validation_context is not None
    assert "Commit-based diff" in context.validation_context
    assert "Changed File Manifest (authoritative):" in context.validation_context
    assert "diff --git a/a.py b/a.py" in context.validation_context
    assert "Agent Changes Summary (supplemental):" in context.validation_context
    assert "agent changes summary shortened due to length" in context.validation_context
    assert context.validation_context.find("diff --git a/a.py") < context.validation_context.find(
        "Agent Changes Summary"
    )


def test_gather_validation_context_reads_mentioned_files_outside_linked_diff(tmp_path) -> None:
    src_dir = tmp_path / "src"
    docs_dir = tmp_path / "docs"
    src_dir.mkdir()
    docs_dir.mkdir()
    (src_dir / "index.ts").write_text("export const registered = 'mcp-tools';")
    (docs_dir / "configuration-audit.md").write_text("mcp-tools audit mapping")

    task = SimpleNamespace(
        id="task-1",
        commits=["abc123"],
        title="Register src/index.ts",
        validation_criteria="Also verify docs/configuration-audit.md.",
        description=None,
    )
    diff_result = SimpleNamespace(
        diff="diff --git a/src/section.tsx b/src/section.tsx\n+change",
        commits=["abc123"],
        file_count=1,
    )

    with (
        patch("gobby.tasks.commits.get_task_diff", return_value=diff_result),
        patch("gobby.tasks.validation.get_validation_context_smart") as smart_context,
    ):
        context = gather_validation_context(
            task=task,
            changes_summary=None,
            repo_path=str(tmp_path),
            task_manager=MagicMock(),
        )

    smart_context.assert_not_called()
    assert context.raw_diff == diff_result.diff
    assert context.validation_context is not None
    assert "Commit-based diff" in context.validation_context
    assert context.file_context_text is not None
    assert "export const registered" in context.file_context_text
    assert "mcp-tools audit mapping" in context.file_context_text
