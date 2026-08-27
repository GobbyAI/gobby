"""Tool handler and Skill-tool interception tests."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.hooks.normalization import normalize_tool_fields
from gobby.skills.formatting import skill_fetch_directive

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestToolHandlers:
    """Test BEFORE_TOOL and AFTER_TOOL handlers."""

    def test_before_tool_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_TOOL allows by default."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)
        assert response.decision == "allow"

    def test_after_tool_allows(self, event_handlers: EventHandlers) -> None:
        """Test AFTER_TOOL allows by default."""
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_after_tool(event)
        assert response.decision == "allow"

    def test_before_tool_allows_gobby_tasks_cli_dict_input(
        self, event_handlers: EventHandlers
    ) -> None:
        """Task CLI policy is enforced by rules, not hardcoded hook logic."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "uv run gobby tasks list --ready"},
            },
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_allows_gobby_tasks_cli_string_input(
        self, event_handlers: EventHandlers
    ) -> None:
        """String shell payloads from app-server adapters are allowed by the hook."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": "gobby tasks list --limit 1"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_allows_gobby_tasks_cli_exec_command_alias(
        self, event_handlers: EventHandlers
    ) -> None:
        """Shell aliases are left to the rules engine."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "exec_command", "tool_input": {"command": "gobby tasks list"}},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_allows_other_gobby_cli_commands(
        self, event_handlers: EventHandlers
    ) -> None:
        """Other gobby CLI commands remain allowed."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": {"command": "uv run gobby status"}},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"


class TestToolHandlerEdgeCases:
    """Test BEFORE_TOOL and AFTER_TOOL edge cases."""

    def test_before_tool_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            metadata={},
        )

        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_records_autonomous_tool_start(self, mock_dependencies: dict) -> None:
        """BEFORE_TOOL marks the call in flight for stagnation detection."""
        progress_tracker = MagicMock()
        handlers = EventHandlers(**mock_dependencies, progress_tracker=progress_tracker)
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": {"command": "uv run pytest tests/foo.py"}},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"
        progress_tracker.record_tool_start.assert_called_once_with(
            session_id="sess-123",
            tool_name="Bash",
            tool_args={"command": "uv run pytest tests/foo.py"},
        )

    def test_after_tool_failure_status(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL handles is_failure metadata."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Write"},
            metadata={"_platform_session_id": "sess-123", "is_failure": True},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_after_tool_tracks_native_outcome_and_skips_wrapper_echo(
        self,
        mock_dependencies: dict,
    ) -> None:
        handlers = EventHandlers(**mock_dependencies)
        native_event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Edit",
                "tool_input": {"file_path": "/repo/a.py"},
                "tool_output": {"success": False, "error": "failed"},
            },
            metadata={"_platform_session_id": "sess-123", "is_failure": True},
        )
        wrapper_event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#123"},
                },
                "tool_output": {"success": False, "error": "failed"},
            },
            metadata={"_platform_session_id": "sess-123", "is_failure": True},
        )

        with patch("gobby.hooks.event_handlers._tool.track_tool_outcome") as track_outcome:
            handlers.handle_after_tool(native_event)
            handlers.handle_after_tool(wrapper_event)

        assert track_outcome.call_count == 1
        assert track_outcome.call_args.args[1:] == ("sess-123", native_event)

    def test_after_tool_no_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            metadata={},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_after_tool_records_autonomous_progress(self, mock_dependencies: dict) -> None:
        """AFTER_TOOL feeds normalized tool traffic to the progress tracker."""
        progress_tracker = MagicMock()
        handlers = EventHandlers(**mock_dependencies, progress_tracker=progress_tracker)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "GOBBY_TEST_PROTECT=1 uv run pytest tests/foo.py"},
                "tool_output": "1 passed",
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        progress_tracker.record_tool_call.assert_called_once_with(
            session_id="sess-123",
            tool_name="Bash",
            tool_args={"command": "GOBBY_TEST_PROTECT=1 uv run pytest tests/foo.py"},
            tool_result="1 passed",
        )

    def test_edit_tracking_failure_logs_warning(
        self,
        mock_dependencies: dict,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """A failed edited-file write remains visible at warning level."""
        import logging

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "src" / "failed.py")},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(tmp_path)

        with (
            caplog.at_level(logging.WARNING),
            patch("gobby.hooks.event_handlers._tool.is_path_gitignored", return_value=False),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files",
                side_effect=RuntimeError("primary write failed"),
            ),
        ):
            handlers.handle_after_tool(event)

        warning = next(
            record
            for record in caplog.records
            if "Failed to process file edit" in record.getMessage()
        )
        assert warning.levelno == logging.WARNING
        assert warning.exc_info is not None

    def test_after_tool_edit_marks_had_edits(self, mock_dependencies: dict, tmp_path: Path) -> None:
        """Test AFTER_TOOL marks had_edits for edit tools on regular files."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "regular" / "file.py")},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(tmp_path)

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_called_once_with("sess-123")
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 1
        assert mock_dependencies["session_storage"].mark_had_edits.call_args is not None

    def test_after_tool_edit_marks_had_edits_for_in_repo_path(
        self, mock_dependencies: dict
    ) -> None:
        """Test AFTER_TOOL marks had_edits when the edited path resolves inside cwd."""
        repo_root = Path("/tmp/project")
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "src/regular.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(repo_root)

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_called_once_with("sess-123")
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 1
        assert mock_dependencies["session_storage"].mark_had_edits.call_args is not None

    @pytest.mark.parametrize(
        ("data", "metadata"),
        [
            (
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "printf changed > src/regular.py"},
                    "canonical_tool_kind": "write",
                    "canonical_repo_mutation": True,
                    "canonical_file_paths": ["src/regular.py"],
                },
                {"_platform_session_id": "sess-123", "is_failure": True},
            ),
            (
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "sed -n 1p src/regular.py"},
                    "canonical_tool_kind": "read",
                    "canonical_file_paths": ["src/regular.py"],
                },
                {"_platform_session_id": "sess-123"},
            ),
            (
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "printf changed > ~/.gobby/bin/gcode"},
                    "canonical_tool_kind": "write",
                    "canonical_repo_mutation": True,
                    "canonical_file_paths": ["~/.gobby/bin/gcode"],
                },
                {"_platform_session_id": "sess-123"},
            ),
        ],
        ids=["failed", "read-only", "external-home-path"],
    )
    def test_after_tool_shell_non_edits_skip_tracking(
        self,
        mock_dependencies: dict,
        data: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(HookEventType.AFTER_TOOL, data=data, metadata=metadata)
        event.cwd = "/tmp/project"

        with patch(
            "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files"
        ) as record_files:
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_not_called()
        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()

    def test_execute_tracks_dirty_bundled_manifest_for_owned_shared_edit(
        self,
        mock_dependencies: dict,
        tmp_path: Path,
    ) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        shared_path = Path("src/gobby/install/shared/skills/example/SKILL.md")
        manifest_path = Path("src/gobby/install/bundled_content_manifest.json")
        (tmp_path / shared_path).parent.mkdir(parents=True)
        (tmp_path / shared_path).write_text("# Example\n", encoding="utf-8")
        (tmp_path / manifest_path).write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "--", str(shared_path), str(manifest_path)],
            cwd=tmp_path,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=tests@gobby.local",
                "-c",
                "user.name=Gobby Tests",
                "commit",
                "-qm",
                "seed",
            ],
            cwd=tmp_path,
            check=True,
        )
        (tmp_path / manifest_path).write_text('{"updated": true}\n', encoding="utf-8")
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "uv run python regenerate_manifest.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(tmp_path)
        task_id = "task-123"
        checkout_root = str(tmp_path.resolve())

        with (
            patch("gobby.hooks.event_handlers._tool.track_tool_outcome"),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.get_variables",
                return_value={
                    "claimed_tasks": {task_id: "#123"},
                    "active_task_id": task_id,
                    "task_edited_files": {task_id: [str(shared_path)]},
                    "task_edited_file_checkouts": {
                        task_id: {checkout_root: [str(shared_path)]},
                    },
                },
            ),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files",
                return_value=True,
            ) as record_files,
        ):
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_called_once_with(
            "sess-123",
            [str(manifest_path)],
            checkout_root=checkout_root,
        )

    @pytest.mark.parametrize(
        "data",
        [
            {
                "tool_name": "Grep",
                "tool_input": {
                    "pattern": "setup/|Textarea|@dagrejs/dagre",
                    "path": "web/src/__tests__",
                },
            },
            {"tool_name": "Read", "tool_input": {"file_path": ".gitattributes"}},
            {"tool_name": "Read", "tool_input": {"file_path": "db/schema.sql"}},
        ],
        ids=["search-pattern-and-directory", "dotfile", "sql-file"],
    )
    def test_read_only_path_like_arguments_skip_tracking(
        self,
        mock_dependencies: dict[str, Any],
        data: dict[str, Any],
    ) -> None:
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data=normalize_tool_fields(data),
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = "/tmp/project"

        with patch(
            "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files"
        ) as record_files:
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_not_called()
        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()

    def test_after_tool_gitignored_edit_skips_tracking(
        self, mock_dependencies: dict, tmp_path: Path
    ) -> None:
        """Gitignored paths stay out of edit ledgers and never mark had_edits."""
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "target" / "output.bin")},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(tmp_path)

        with (
            patch("gobby.hooks.event_handlers._tool.is_path_gitignored", return_value=True),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files"
            ) as record_files,
        ):
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_not_called()
        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        mock_dependencies["task_manager"].list_tasks.assert_not_called()

    def test_after_tool_non_ignored_edit_still_marks_had_edits(
        self, mock_dependencies: dict, tmp_path: Path
    ) -> None:
        """Paths git does not ignore keep the existing tracking behavior."""
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "src" / "main.py")},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(tmp_path)

        with (
            patch("gobby.hooks.event_handlers._tool.is_path_gitignored", return_value=False),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files",
                return_value=True,
            ) as record_files,
        ):
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_called_once_with(
            "sess-123",
            ["src/main.py"],
            checkout_root=str(tmp_path),
        )
        mock_dependencies["session_storage"].mark_had_edits.assert_called_once_with("sess-123")

    def test_structured_multi_file_edit_is_recorded_atomically(
        self,
        mock_dependencies: dict,
        tmp_path: Path,
    ) -> None:
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        notify_code_index = MagicMock()
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_repo_mutation": True,
                "canonical_structured_mutation": True,
                "canonical_file_paths": [
                    str(tmp_path / "src" / "first.py"),
                    str(tmp_path / "docs" / "plan.md"),
                    "src/first.py",
                ],
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(tmp_path)

        with (
            patch.object(handlers, "_notify_code_index", notify_code_index),
            patch("gobby.hooks.event_handlers._tool.is_path_gitignored", return_value=False),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files",
                return_value=True,
            ) as record_files,
        ):
            response = handlers.handle_after_tool(event)

        record_files.assert_called_once_with(
            "sess-123",
            ["src/first.py", "docs/plan.md"],
            checkout_root=str(tmp_path),
        )
        assert response.decision == "allow"
        assert notify_code_index.call_count == 2

    def test_structured_edit_without_paths_skips_tracking_and_warns(
        self,
        mock_dependencies: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "WriteFile",
                "canonical_tool_kind": "write",
                "canonical_repo_mutation": True,
                "canonical_structured_mutation": True,
                "canonical_file_paths": [],
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        with (
            caplog.at_level("WARNING"),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files",
                return_value=True,
            ) as record_files,
        ):
            handlers.handle_after_tool(event)

        record_files.assert_not_called()
        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert "no attributable file paths" in caplog.text

    def test_failed_structured_edit_does_not_change_attribution(
        self,
        mock_dependencies: dict,
    ) -> None:
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_repo_mutation": True,
                "canonical_structured_mutation": True,
                "canonical_file_paths": ["src/failed.py"],
            },
            metadata={"_platform_session_id": "sess-123", "is_failure": True},
        )

        with patch(
            "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files"
        ) as record_files:
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_not_called()
        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()

    def test_after_tool_absolute_path_without_repo_context_not_tracked(
        self, mock_dependencies: dict
    ) -> None:
        """Out-of-repo absolute paths without cwd are not attributed as repo edits."""
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "/private/tmp/scratchpad/notes.md"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        with patch(
            "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files"
        ) as record_files:
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_not_called()
        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()

    def test_absolute_path_in_same_project_worktree_tracks_target_checkout(
        self,
        mock_dependencies: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        project_id = "21000000-0000-4000-8000-000000000073"
        main_root = tmp_path / "main"
        worktree_root = tmp_path / "worktree"
        for root in (main_root, worktree_root):
            (root / ".gobby").mkdir(parents=True)
            (root / ".gobby" / "project.json").write_text(
                f'{{"id": "{project_id}"}}',
                encoding="utf-8",
            )
        target = worktree_root / "src" / "owned.py"
        target.parent.mkdir(parents=True)
        target.write_text("owned = True\n", encoding="utf-8")
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(main_root)
        event.project_id = project_id

        with (
            patch("gobby.hooks.event_handlers._tool.is_path_gitignored", return_value=False),
            patch(
                "gobby.hooks.event_handlers._tool.SessionVariableManager.record_edited_files",
                return_value=True,
            ) as record_files,
        ):
            response = handlers.handle_after_tool(event)

        assert response.decision == "allow"
        record_files.assert_called_once_with(
            "sess-123",
            ["src/owned.py"],
            checkout_root=str(worktree_root.resolve()),
        )

    def test_after_tool_notifies_code_index_with_project_root_path(
        self, mock_dependencies: dict[str, Any], tmp_path: Path
    ) -> None:
        """Test code index notification uses project root even when cwd is nested."""
        repo_root = tmp_path / "project"
        deep_cwd = repo_root / "src" / "pkg"
        (repo_root / ".gobby").mkdir(parents=True)
        (repo_root / ".gobby" / "project.json").write_text('{"id": "proj-1"}')
        deep_cwd.mkdir(parents=True)
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        code_index_trigger = MagicMock()
        resolve_project_id = MagicMock(return_value="proj-1")
        handlers = EventHandlers(
            **mock_dependencies,
            code_index_trigger=code_index_trigger,
            resolve_project_id=resolve_project_id,
        )
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "edited.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(deep_cwd)

        handlers.handle_after_tool(event)

        resolve_project_id.assert_called_once_with(None, str(repo_root.resolve()))
        assert resolve_project_id.call_count == 1
        assert resolve_project_id.call_args is not None
        code_index_trigger.notify_file_changed.assert_called_once_with(
            file_path="src/pkg/edited.py",
            project_id="proj-1",
            root_path=str(repo_root.resolve()),
            code_overlay_project_id=None,
        )
        assert code_index_trigger.notify_file_changed.call_count == 1
        assert code_index_trigger.notify_file_changed.call_args is not None

    def test_after_tool_edit_skips_gobby_internal_files(
        self,
        mock_dependencies: dict[str, Any],
    ) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for .gobby/ internal files."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "/path/to/project/.gobby/tasks.jsonl"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 0
        assert not mock_dependencies["session_storage"].mark_had_edits.called

    def test_after_tool_edit_skips_out_of_repo_paths(
        self,
        mock_dependencies: dict[str, Any],
    ) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for edits outside cwd."""
        repo_root = Path("/tmp/project")
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "../outside/settings.json"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(repo_root)

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 0
        assert not mock_dependencies["session_storage"].mark_had_edits.called

    def test_after_tool_edit_skips_relative_gobby_path(
        self,
        mock_dependencies: dict[str, MagicMock],
    ) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for relative .gobby/ paths."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Edit",
                "tool_input": {"file_path": ".gobby/memories.jsonl"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 0
        assert not mock_dependencies["session_storage"].mark_had_edits.called


class TestSkillToolInterception:
    """Tests for Skill tool call interception in handle_before_tool."""

    @pytest.fixture
    def parsed_skill(self) -> Any:
        """Create a mock ParsedSkill for testing."""
        from gobby.skills.parser import ParsedSkill

        return ParsedSkill(
            name="build-coordinator",
            description="Inspect Gobby agent progress through supported MCP tools.",
            content="# Agent Monitoring\nInspect agent progress.",
        )

    @pytest.fixture
    def skill_manager(self, parsed_skill: Any) -> MagicMock:
        """Create a mock skill manager that resolves build-coordinator."""
        manager = MagicMock()
        manager.resolve_skill_name.return_value = parsed_skill
        return manager

    @pytest.fixture
    def handlers_with_skills(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> EventHandlers:
        """EventHandlers with a skill manager configured."""
        mock_dependencies["skill_manager"] = skill_manager
        return EventHandlers(**mock_dependencies)

    def test_skill_tool_resolves_gobby_skill(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Skill tool call with a gobby skill name blocks with fetch directive."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "build-coordinator"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "block"
        assert skill_fetch_directive("build-coordinator") in (response.context or "")
        assert "# Agent Monitoring" not in (response.context or "")
        assert "<skill-context" not in (response.context or "")
        skill_manager.resolve_skill_name.assert_called_once_with(
            "build-coordinator",
            project_id="",
        )

    def test_skill_tool_with_gobby_prefix(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Skill tool call with gobby: prefix strips it before resolving."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "gobby:build-coordinator"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "block"
        assert skill_fetch_directive("build-coordinator") in (response.context or "")
        skill_manager.resolve_skill_name.assert_called_once_with(
            "build-coordinator",
            project_id="",
        )

    def test_skill_tool_with_args(self, handlers_with_skills: EventHandlers) -> None:
        """Skill tool call with args includes them in context."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Skill",
                "tool_input": {"skill": "build-coordinator", "args": "status"},
            },
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "block"
        assert "User arguments: status" in response.context

    def test_skill_tool_unknown_allows_native_handler(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Unknown Skill names pass through to the native handler."""
        skill_manager.resolve_skill_name.return_value = None
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "unknown-thing"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        skill_manager.resolve_skill_name.assert_called_once_with(
            "unknown-thing",
            project_id="",
        )

    def test_skill_tool_non_gobby_namespace(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Skill tool call with non-gobby namespace is not intercepted."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "ms-office-suite:pdf"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        skill_manager.resolve_skill_name.assert_not_called()

    def test_non_skill_tool_unaffected(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Non-Skill tool calls are unaffected."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": {"command": "ls"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        skill_manager.resolve_skill_name.assert_not_called()

    def test_skill_tool_no_skill_manager(self, mock_dependencies: dict[str, Any]) -> None:
        """Skill tool call without skill_manager passes through."""
        handlers = EventHandlers(**mock_dependencies)  # no skill_manager
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "build-coordinator"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_skill_tool_programming_error_propagates(
        self,
        handlers_with_skills: EventHandlers,
        skill_manager: MagicMock,
    ) -> None:
        """Programming errors during skill resolution are not swallowed."""
        skill_manager.resolve_skill_name.side_effect = RuntimeError("boom")
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "build-coordinator"}},
        )

        with pytest.raises(RuntimeError, match="boom"):
            handlers_with_skills.handle_before_tool(event)

    def test_skill_tool_resolution_failure_allows_native_handler(
        self,
        handlers_with_skills: EventHandlers,
        skill_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected resolution failures are logged and fall through."""
        skill_manager.resolve_skill_name.side_effect = FileNotFoundError("temporary miss")
        caplog.set_level(logging.WARNING)
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "build-coordinator"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        assert "Failed to resolve Skill tool call" in caplog.text
        assert any(record.exc_info is not None for record in caplog.records)

    def test_skill_tool_unexpected_os_error_is_not_swallowed(
        self,
        handlers_with_skills: EventHandlers,
        skill_manager: MagicMock,
    ) -> None:
        """Unexpected generic OS errors should keep their traceback."""
        skill_manager.resolve_skill_name.side_effect = OSError("disk full")
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "build-coordinator"}},
        )

        with pytest.raises(OSError, match="disk full"):
            handlers_with_skills.handle_before_tool(event)

    def test_skill_tool_value_error_is_not_swallowed(
        self,
        handlers_with_skills: EventHandlers,
        skill_manager: MagicMock,
    ) -> None:
        """ValueError means the resolver rejected input, not an expected transient miss."""
        skill_manager.resolve_skill_name.side_effect = ValueError("bad skill payload")
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "build-coordinator"}},
        )

        with pytest.raises(ValueError, match="bad skill payload"):
            handlers_with_skills.handle_before_tool(event)

    def test_skill_tool_tier2_mcp_fallback(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> None:
        """Tier 2: When local resolve fails, falls back to gobby-skills MCP get_skill."""
        skill_manager.resolve_skill_name.return_value = None
        mock_call_tool = MagicMock(
            return_value={
                "success": True,
                "skill": {"name": "playwright", "content": "# Playwright\nBrowser automation."},
            }
        )
        mock_dependencies["skill_manager"] = skill_manager
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "playwright"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "block"
        assert skill_fetch_directive("playwright") in (response.context or "")
        assert "Browser automation" not in (response.context or "")
        assert "<skill-context" not in (response.context or "")
        mock_call_tool.assert_any_call("gobby-skills", "get_skill", {"name": "playwright"})

    def test_skill_tool_hub_match_not_searched_for_native_loop(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> None:
        """Hub-only matches are not searched; native Skill names pass through."""
        skill_manager.resolve_skill_name.return_value = None

        def _mock_call(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            if tool == "get_skill":
                return {"success": False}
            if tool == "search_hub":
                return {
                    "success": True,
                    "results": [
                        {
                            "display_name": "playwright-cli",
                            "slug": "playwright-cli",
                            "description": "Browser automation via Playwright",
                            "hub_name": "clawdhub",
                        }
                    ],
                }
            return {"success": False}

        mock_call_tool = MagicMock(side_effect=_mock_call)
        mock_dependencies["skill_manager"] = skill_manager
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "/loop"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"
        mock_call_tool.assert_called_once_with("gobby-skills", "get_skill", {"name": "/loop"})
        assert all(call.args[1] != "search_hub" for call in mock_call_tool.call_args_list)

    def test_skill_tool_unresolved_name_allows_native_handler(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> None:
        """Unresolved names pass through after local and MCP misses."""
        skill_manager.resolve_skill_name.return_value = None

        def _mock_call(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": False}

        mock_call_tool = MagicMock(side_effect=_mock_call)
        mock_dependencies["skill_manager"] = skill_manager
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "nonexistent"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"
        mock_call_tool.assert_called_once_with("gobby-skills", "get_skill", {"name": "nonexistent"})

    def test_skill_tool_no_manager_but_has_call_tool(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Without skill_manager but with call_tool, tier 2 still works."""
        mock_call_tool = MagicMock(
            return_value={
                "success": True,
                "skill": {"name": "playwright", "content": "# Playwright skill"},
            }
        )
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)  # no skill_manager

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "playwright"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "block"
        assert skill_fetch_directive("playwright") in (response.context or "")
