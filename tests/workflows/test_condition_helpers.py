"""Tests for condition helper functions used in rule engine expressions."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.condition_helpers import (
    _normalize_task_id,
    all_tasks_have_label,
    first_tdd_code_path,
    first_tdd_test_path,
    is_gobby_build_command,
    is_task_complete,
    shell_command_invokes_gcode,
    task_commit_project_path_allowlist_violation,
    task_needs_human_review,
    task_tree_complete,
    task_type_in,
    touches_claude_memory_path,
    touches_docker_policy_path,
    touches_ui_design_path,
)

pytestmark = pytest.mark.unit


def _manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


def _task(
    manager: LocalTaskManager,
    sample_project: dict[str, Any],
    **kwargs: Any,
) -> Task:
    title = kwargs.pop("title", "Condition helper task")
    kwargs.setdefault("validation_criteria", "Test task completion is observable.")
    return manager.create_task(
        project_id=sample_project["id"],
        title=title,
        **kwargs,
    )


def _start_development_stage(manager: LocalTaskManager, task_id: str) -> None:
    manager.initialize_task_manifest(task_id)
    manager.stage_states.start_stage(task_id, "development", by_session_id=None)


def _seq_ref(task: Task) -> str:
    assert task.seq_num is not None
    return f"#{task.seq_num}"


class TestIsGobbyBuildCommand:
    @pytest.mark.parametrize(
        "command",
        [
            "gobby build #15117",
            "/Users/josh/.gobby/bin/gobby build #15117 --clone",
            "GOBBY_TEST_PROTECT=1 uv run --frozen gobby build #15117",
            "uv run -- gobby build docs/plan.md --quick",
            "python -m gobby build #15117",
            "python -u -m gobby build #15117",
            "ruff check src && gobby build #15117",
        ],
    )
    def test_detects_direct_gobby_build_invocations(self, command: str) -> None:
        assert is_gobby_build_command(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "",
            None,
            'rg "gobby build" src tests',
            "uv run gobby status",
            "gobby status",
            "python -m pytest tests/cli/test_cli_build.py",
        ],
    )
    def test_skips_non_build_invocations(self, command: object) -> None:
        assert is_gobby_build_command(command) is False


class TestShellCommandInvokesGcode:
    @pytest.mark.parametrize(
        "command",
        [
            'gcode grep "pattern" src -m 50',
            'gcode search "query" | python3 -c "print(1)"',
            "cd repo && /usr/local/bin/gcode outline src/app.py",
            'GCODE_LOG=debug gcode search-content "query"',
        ],
    )
    def test_detects_gcode_command_segments(self, command: str) -> None:
        assert shell_command_invokes_gcode(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "",
            None,
            "rg gcode src",
            "python3 -c \"print('gcode')\"",
            "gcodex search query",
        ],
    )
    def test_skips_non_gcode_invocations(self, command: object) -> None:
        assert shell_command_invokes_gcode(command) is False


class TestNormalizeTaskId:
    def test_int_to_hash_format(self) -> None:
        assert _normalize_task_id(9438) == "#9438"

    def test_zero(self) -> None:
        assert _normalize_task_id(0) == "#0"

    def test_string_passthrough(self) -> None:
        assert _normalize_task_id("#9438") == "#9438"

    def test_uuid_passthrough(self) -> None:
        uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert _normalize_task_id(uuid) == uuid

    def test_invalid_uuid_bytes_fall_back_to_string(self) -> None:
        assert _normalize_task_id(b"short") == "b'short'"


class TestIsTaskComplete:
    def test_closed_is_complete(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        closed = manager.close_task(task.id, force=True)

        assert not hasattr(closed, "status")
        assert is_task_complete(closed) is True

    def test_ready_is_not_complete(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        task = _task(_manager(temp_db), sample_project)
        assert is_task_complete(task) is False

    def test_in_progress_is_not_complete(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        _start_development_stage(manager, task.id)

        assert is_task_complete(manager.get_task(task.id)) is False

    def test_needs_review_is_not_complete(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        _start_development_stage(manager, task.id)
        reviewed = manager.submit_for_review(task.id)

        assert is_task_complete(reviewed) is False

    def test_escalated_is_not_complete(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        escalated = manager.escalate_task(task.id, reason="needs human")

        assert is_task_complete(escalated) is False


class TestTaskTreeCompleteIntHandling:
    def test_int_task_id_closed(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.close_task(task.id, force=True)

        assert task_tree_complete(manager, task.seq_num) is True

    def test_int_task_id_open(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_tree_complete(manager, task.seq_num) is False

    def test_int_task_id_not_found(self, temp_db: HubDatabase) -> None:
        assert task_tree_complete(_manager(temp_db), 9438) is False

    def test_int_does_not_raise_type_error(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_tree_complete(manager, task.seq_num) is False


def test_task_needs_human_review_returns_false_for_invalid_uuid_bytes(
    temp_db: HubDatabase,
) -> None:
    assert task_needs_human_review(_manager(temp_db), b"short") is False


class TestTaskTreeCompleteString:
    def test_string_task_id_closed(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.close_task(task.id, force=True)

        assert task_tree_complete(manager, task.id) is True

    def test_string_task_id_open(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_tree_complete(manager, task.id) is False


class TestTaskTreeCompleteList:
    def test_list_of_strings_all_closed(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, [first.id, second.id]) is True

    def test_list_of_ints_all_closed(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, [first.seq_num, second.seq_num]) is True

    def test_mixed_list(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, [first.id, second.seq_num]) is True

    def test_list_one_open(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)

        assert task_tree_complete(manager, [first.seq_num, second.seq_num]) is False


class TestTaskTreeCompleteIterableInputs:
    def test_tuple_of_strings_all_closed(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, (first.id, second.id)) is True

    def test_generator_of_ints_all_closed(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, (task.seq_num for task in (first, second))) is True

    @pytest.mark.parametrize(
        "bytes_factory",
        [
            bytes,
            bytearray,
            memoryview,
        ],
    )
    def test_bytes_like_uuid_is_scalar_task_id(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
        bytes_factory: Callable[[bytes], bytes | bytearray | memoryview],
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.close_task(task.id, force=True)

        assert task_tree_complete(manager, bytes_factory(UUID(task.id).bytes)) is True


class TestTaskTreeCompleteEdgeCases:
    def test_none_returns_true(self, temp_db: HubDatabase) -> None:
        assert task_tree_complete(_manager(temp_db), None) is True

    def test_no_task_manager_returns_false(self) -> None:
        assert task_tree_complete(None, "#1") is False

    def test_subtree_all_closed(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child_a = _task(manager, sample_project, title="Child A", parent_task_id=parent.id)
        child_b = _task(manager, sample_project, title="Child B", parent_task_id=parent.id)
        manager.close_task(child_a.id, force=True)
        manager.close_task(child_b.id, force=True)

        assert task_tree_complete(manager, parent.id) is True

    def test_subtree_one_open(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child_a = _task(manager, sample_project, title="Child A", parent_task_id=parent.id)
        _task(manager, sample_project, title="Child B", parent_task_id=parent.id)
        manager.close_task(child_a.id, force=True)

        assert task_tree_complete(manager, parent.id) is False

    def test_nested_subtree(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child = _task(manager, sample_project, title="Child", parent_task_id=parent.id)
        grandchild = _task(manager, sample_project, title="Grandchild", parent_task_id=child.id)
        manager.close_task(grandchild.id, force=True)

        assert task_tree_complete(manager, parent.id) is True

    def test_nested_subtree_incomplete(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child = _task(manager, sample_project, title="Child", parent_task_id=parent.id)
        _task(manager, sample_project, title="Grandchild", parent_task_id=child.id)

        assert task_tree_complete(manager, parent.id) is False


class TestTaskNeedsHumanReview:
    def test_int_task_id_escalated(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.escalate_task(task.id, reason="needs human")

        assert task_needs_human_review(manager, task.seq_num) is True

    def test_int_task_id_not_escalated(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_needs_human_review(manager, task.seq_num) is False

    def test_needs_review_is_not_human_review(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        _start_development_stage(manager, task.id)
        manager.submit_for_review(task.id)

        assert task_needs_human_review(manager, task.id) is False

    def test_string_task_id(self, temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.escalate_task(task.id, reason="needs human")

        assert task_needs_human_review(manager, task.id) is True

    def test_none_returns_false(self, temp_db: HubDatabase) -> None:
        assert task_needs_human_review(_manager(temp_db), None) is False

    def test_no_manager_returns_false(self) -> None:
        assert task_needs_human_review(None, "#100") is False


class TestTaskTypeIn:
    def test_matches_epic_by_uuid(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, task.id, "epic") is True

    def test_matches_epic_by_uuid_instance(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, UUID(task.id), "epic") is True

    def test_matches_epic_by_raw_uuid_bytes(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, UUID(task.id).bytes, "epic") is True

    def test_matches_epic_by_raw_uuid_bytearray(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, bytearray(UUID(task.id).bytes), "epic") is True

    def test_matches_epic_by_hash_ref(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, _seq_ref(task), "epic") is True

    def test_matches_epic_by_int_seq_ref(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, task.seq_num, "epic") is True

    def test_matches_mixed_list_when_any_task_type_matches(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        normal = _task(manager, sample_project, title="Normal", task_type="task")
        epic = _task(manager, sample_project, title="Epic", task_type="epic")

        assert task_type_in(manager, [normal.id, epic.seq_num], "epic") is True

    def test_returns_false_for_bytes_ref_without_iterating_bytes(
        self, temp_db: HubDatabase
    ) -> None:
        assert task_type_in(_manager(temp_db), b"#1", "epic") is False

    def test_returns_false_for_invalid_uuid_bytes_in_list(self, temp_db: HubDatabase) -> None:
        assert task_type_in(_manager(temp_db), [b"#1"], "epic") is False

    def test_returns_false_for_missing_task(self, temp_db: HubDatabase) -> None:
        assert task_type_in(_manager(temp_db), "#999999", "epic") is False

    def test_returns_false_without_task_manager(self) -> None:
        assert task_type_in(None, "#1", "epic") is False

    def test_returns_false_for_non_matching_type(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="task")

        assert task_type_in(manager, task.id, "epic") is False

    def test_matches_persisted_task_type_with_surrounding_whitespace(self) -> None:
        manager = SimpleNamespace(
            db=None,
            get_task=lambda _task_id: SimpleNamespace(task_type=" Epic "),
        )

        assert task_type_in(manager, "task-id", "epic") is True


class TestTaskCommitProjectPathAllowlistViolation:
    def test_blocks_raw_project_path_cwd_in_task_commits(self) -> None:
        event_data = {
            "canonical_file_path": "/repo/src/gobby/mcp_proxy/tools/task_commits.py",
        }
        tool_input = {
            "file_path": "src/gobby/mcp_proxy/tools/task_commits.py",
            "new_string": (
                "task = task_manager.unlink_commit(resolved_task_id, commit_sha, cwd=project_path)"
            ),
        }

        assert task_commit_project_path_allowlist_violation(event_data, tool_input) is True

    def test_blocks_raw_project_path_cwd_in_lifecycle_close(self) -> None:
        event_data = {
            "canonical_file_path": "/repo/src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py",
        }
        tool_input = {
            "file_path": "src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py",
            "edits": [
                {
                    "old_string": "safe",
                    "new_string": (
                        "current_commit_sha = normalize_commit_sha(commit_sha, cwd=project_path)"
                    ),
                }
            ],
        }

        assert task_commit_project_path_allowlist_violation(event_data, tool_input) is True

    def test_allows_resolver_first_cwd(self) -> None:
        event_data = {
            "canonical_file_path": "/repo/src/gobby/mcp_proxy/tools/task_commits.py",
        }
        tool_input = {
            "file_path": "src/gobby/mcp_proxy/tools/task_commits.py",
            "content": """
                repo_path = resolve_task_repo_path(
                    task_manager=task_manager,
                    project_manager=project_manager,
                    task=task,
                    project_path=project_path,
                )
                task = task_manager.link_commit(resolved_task_id, commit_sha, cwd=repo_path)
            """,
        }

        assert task_commit_project_path_allowlist_violation(event_data, tool_input) is False

    def test_allows_existing_get_task_and_repo_path_pattern(self) -> None:
        event_data = {
            "canonical_file_path": "/repo/src/gobby/mcp_proxy/tools/task_commits.py",
        }
        tool_input = {
            "file_path": "src/gobby/mcp_proxy/tools/task_commits.py",
            "content": """
                task_and_repo_path = _get_task_and_repo_path(
                    resolved_task_id, task_id, project_path
                )
                if isinstance(task_and_repo_path, dict):
                    return task_and_repo_path
                _, repo_path = task_and_repo_path
                result = get_task_diff_fn(task_id=resolved_task_id, cwd=repo_path)
            """,
        }

        assert task_commit_project_path_allowlist_violation(event_data, tool_input) is False

    def test_allows_unrelated_files(self) -> None:
        event_data = {"canonical_file_path": "/repo/src/gobby/other.py"}
        tool_input = {
            "file_path": "src/gobby/other.py",
            "new_string": "run_git_command(['git', 'status'], cwd=project_path)",
        }

        assert task_commit_project_path_allowlist_violation(event_data, tool_input) is False


class TestTddPathHelpers:
    def test_first_tdd_code_path_uses_canonical_paths(self) -> None:
        event_data = {
            "canonical_file_paths": ["tests/test_app.py", "src/app.py"],
        }

        assert first_tdd_code_path(event_data, {}) == "src/app.py"

    def test_first_tdd_code_path_skips_test_and_special_python_files(self) -> None:
        event_data = {
            "canonical_file_paths": [
                "src/__init__.py",
                "tests/helper.py",
                "src/conftest.py",
                "src/test_helper.py",
                "src/helper_test.py",
            ],
        }

        assert first_tdd_code_path(event_data, {}) == ""

    def test_first_tdd_test_path_uses_canonical_paths(self) -> None:
        event_data = {
            "canonical_file_paths": ["src/app.py", "tests/helper.py"],
        }

        assert first_tdd_test_path(event_data, {}) == "tests/helper.py"

    def test_tdd_helpers_fall_back_to_native_tool_input(self) -> None:
        tool_input = {"file_path": "src/new_module.py"}

        assert first_tdd_code_path({}, tool_input) == "src/new_module.py"


class TestTouchesClaudeMemoryPath:
    def test_matches_canonical_path(self) -> None:
        event_data = {
            "canonical_file_path": ".claude/memory/project.md",
        }

        assert touches_claude_memory_path(event_data, {}) is True

    def test_matches_search_path_field(self) -> None:
        tool_input = {"path": ".claude/memory"}

        assert touches_claude_memory_path({}, tool_input) is True

    def test_skips_non_memory_claude_path(self) -> None:
        tool_input = {"file_path": ".claude/plans/design.md"}

        assert touches_claude_memory_path({}, tool_input) is False


class TestTouchesDockerPolicyPath:
    @pytest.mark.parametrize(
        "path",
        [
            "Dockerfile",
            "containers/Dockerfile.dev",
            "containers/dev.Dockerfile",
            "containers/web.Dockerfile",
            "containers/Dockerfile-dev",
            "containers/Containerfile",
            "deploy/docker-compose.services.yml",
            "deploy/podman-compose.override.yml",
            "deploy/compose.prod.yaml",
            "deploy/compose-dev.yml",
            ".dockerignore",
            "docker-bake.hcl",
            "docker-bake.override.hcl",
            ".docker/config.json",
        ],
    )
    def test_matches_docker_policy_paths(self, path: str) -> None:
        assert touches_docker_policy_path({"canonical_file_path": path}, {}) is True

    def test_matches_protected_path_in_multi_file_event(self) -> None:
        event_data = {
            "canonical_file_paths": ["README.md", "ops/docker-compose.yml"],
        }

        assert touches_docker_policy_path(event_data, {}) is True

    def test_skips_unrelated_paths(self) -> None:
        event_data = {"canonical_file_paths": ["README.md", "config/app.yaml", "docker-bakery.hcl"]}

        assert touches_docker_policy_path(event_data, {}) is False


class TestTouchesUiDesignPath:
    @pytest.mark.parametrize(
        "path",
        [
            "/project/src/components/Button.tsx",
            "web/src/lib/api.ts",
            "/Users/dev/repo/web/scripts/build.mjs",
        ],
    )
    def test_matches_ui_paths(self, path: str) -> None:
        assert touches_ui_design_path({"canonical_file_path": path}, {}) is True

    def test_matches_any_path_in_multi_file_event(self) -> None:
        event_data = {
            "canonical_file_paths": ["README.md", None, "web/src/lib/api.ts"],
        }

        assert touches_ui_design_path(event_data, {}) is True

    def test_ignores_non_string_entries(self) -> None:
        event_data = {"canonical_file_paths": [None, 12, {"path": "web/src/App.tsx"}]}

        assert touches_ui_design_path(event_data, {}) is False

    def test_skips_non_ui_script_paths(self) -> None:
        event_data = {
            "canonical_file_paths": [
                "src/gobby/install/shared/skills/impeccable/scripts/hook.mjs",
                "/project/eslint.config.js",
                "/project/README.md",
            ]
        }

        assert touches_ui_design_path(event_data, {}) is False


class TestAllTasksHaveLabel:
    def test_requires_every_persisted_task_to_carry_label(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        manager = _manager(temp_db)
        labeled = _task(manager, sample_project, labels=["live-session"])
        ordinary = _task(manager, sample_project, labels=["ordinary"])

        assert all_tasks_have_label(manager, [labeled.id], "live-session")
        assert not all_tasks_have_label(
            manager,
            [labeled.id, f"#{ordinary.seq_num}"],
            "live-session",
        )

    def test_fails_closed_for_empty_missing_or_unavailable_tasks(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, labels=["live-session"])

        assert not all_tasks_have_label(manager, [], "live-session")
        assert not all_tasks_have_label(manager, [task.id, "#999999999"], "live-session")
        assert not all_tasks_have_label(manager, cast(Any, object()), "live-session")
        assert not all_tasks_have_label(None, [task.id], "live-session")

    def test_reads_current_database_labels_on_each_evaluation(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, labels=["live-session"])

        assert all_tasks_have_label(manager, [task.id], "live-session")
        manager.remove_label(task.id, "live-session")
        assert not all_tasks_have_label(manager, [task.id], "live-session")

    def test_skips_non_claude_memory_path(self) -> None:
        event_data = {
            "canonical_file_paths": ["docs/memory/project.md"],
        }

        assert touches_claude_memory_path(event_data, {}) is False

    def test_matches_user_level_auto_memory(self) -> None:
        tool_input = {
            "file_path": "/Users/josh/.claude/projects/-Users-josh-Projects-gobby/memory/MEMORY.md",
        }

        assert touches_claude_memory_path({}, tool_input) is True

    def test_matches_user_level_auto_memory_directory(self) -> None:
        tool_input = {"path": "/Users/josh/.claude/projects/-Users-josh-Projects-gobby/memory"}

        assert touches_claude_memory_path({}, tool_input) is True

    def test_skips_repo_memory_source_inside_claude_worktree(self) -> None:
        # Regression (#17585): a repo checked out under .claude/worktrees/
        # contains both ".claude/" and "src/gobby/memory/" in one path.
        tool_input = {
            "file_path": (
                "/Users/josh/Projects/gobby/.claude/worktrees/task-17495"
                "/src/gobby/memory/dream/truth_digest.py"
            ),
        }

        assert touches_claude_memory_path({}, tool_input) is False

    def test_skips_session_transcript_beside_auto_memory(self) -> None:
        tool_input = {
            "file_path": (
                "/Users/josh/.claude/projects/-Users-josh-Projects-gobby"
                "/6426e431-97f9-4c78-8adc-81f648ce95ad.jsonl"
            ),
        }

        assert touches_claude_memory_path({}, tool_input) is False
