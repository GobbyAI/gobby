"""Generic touched-path and plan-artifact helpers."""

import pytest

from gobby.providers import provider_metadata
from gobby.workflows.enforcement.blocking import TASK_MUTATION_TOOLS_BY_SERVER

pytestmark = pytest.mark.unit


def test_retired_tools_absent_from_enforcement_lists() -> None:
    retired = {
        "release_task_paths",
        "inspect_task_path_ownership",
        "record_close_receipt",
        "transfer_task_authority",
        "record_closing_step",
        "record_implementation_step",
        "record_review_step",
        "record_test_step",
    }
    assert retired.isdisjoint(TASK_MUTATION_TOOLS_BY_SERVER["gobby-tasks"])


class TestIsPlanFile:
    """Unit tests for is_plan_file helper."""

    def test_gobby_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/.gobby/plans/my-plan.md") is True

    def test_claude_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.claude/plans/design.md") is True

    def test_non_md_file_in_plans_dir(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/.gobby/plans/notes.txt") is False

    def test_regular_source_file(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/src/main.py") is False

    def test_empty_path(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("") is False

    def test_md_file_outside_plans(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/docs/plan.md") is False

    def test_source_param_accepted(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/.gobby/plans/x.md", "claude_code") is True
        assert is_plan_file("/project/.gobby/plans/x.md", None) is True

    def test_codex_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.codex/plans/plan.md") is True

    def test_codex_config_toml_rejected(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.codex/config.toml") is False

    @pytest.mark.parametrize(
        "directory",
        [".gobby", *(entry.user_directory for entry in provider_metadata())],
    )
    def test_markdown_under_every_provider_user_directory_is_a_plan_file(
        self,
        directory: str,
    ) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file(f"/home/user/{directory}/notes/plan.md") is True


class TestTouchedFileHelpers:
    """Unit tests for touched-path task gating helpers."""

    def test_get_touched_file_paths_prefers_file_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import get_touched_file_paths

        result = get_touched_file_paths(
            {
                "file_paths": ["/project/a.py", "/project/b.py"],
                "file_path": "/project/ignored.py",
            }
        )

        assert result == ["/project/a.py", "/project/b.py"]

    def test_get_touched_file_paths_falls_back_to_changes(self) -> None:
        from gobby.workflows.enforcement.blocking import get_touched_file_paths

        result = get_touched_file_paths(
            {
                "changes": [
                    {"path": "/project/a.py"},
                    {"file_path": "/project/b.py"},
                    {"path": "/project/a.py"},
                ]
            }
        )

        assert result == ["/project/a.py", "/project/b.py"]

    def test_requires_task_for_any_touched_file_allows_all_exempt_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {
                "file_paths": [
                    "/project/.gobby/plans/plan.md",
                    "/project/.codex/notes.md",
                ]
            },
            source="codex",
            plan_mode=False,
        )

        assert result is False

    def test_requires_task_for_retired_feedback_inbox_markdown(self) -> None:
        """The taskless feedback-inbox carve-out is retired; inbox writes need a task."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {
                "file_paths": [
                    "docs/research/gobby-feedback/inbox/2026-08-26-codex-session-11117.md",
                ]
            },
            source="codex",
            plan_mode=False,
        )

        assert result is True

    def test_requires_task_for_any_touched_file_blocks_mixed_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {
                "file_paths": [
                    "/project/.gobby/plans/plan.md",
                    "/project/src/main.py",
                ]
            },
            source="codex",
            plan_mode=False,
        )

        assert result is True

    def test_requires_task_for_any_touched_file_allows_markdown_in_plan_mode(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"file_paths": ["/project/docs/notes.md"]},
            source="codex",
            plan_mode=True,
        )

        assert result is False

    def test_requires_task_for_any_touched_file_fails_closed_without_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"patch": "*** Begin Patch\n*** End Patch\n"},
            source="codex",
            plan_mode=False,
        )

        assert result is True
