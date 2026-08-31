from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers import EDIT_TOOLS, EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.session_types import HookSessionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.observer_commits import detect_bash_commit
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import add_claimed_task
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.integration

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_edit_history_flow(temp_db, tmp_path) -> None:
    """Test full flow: session -> claim task -> edit -> had_edits set."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    in_repo_file = repo_root / "src" / "edited.py"
    in_repo_file.parent.mkdir(parents=True)

    # 1. Setup managers
    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)

    # Create project to satisfy FK
    project = install_isolated_checkout_project(
        temp_db, repo_root, name="test-project", machine_id=LOCAL_MACHINE_ID
    ).project
    project_id = project.id

    # EventHandlers needs session_storage and task_manager
    handlers = EventHandlers(
        session_storage=session_manager,
        task_manager=task_manager,
    )

    # 2. Register a session
    session = session_manager.register(
        external_id="test-session-1",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
        title="Test Session",
    )
    assert not session.had_edits

    # 3. Create a task
    task = task_manager.create_task(
        project_id=project_id,
        title="Test Task",
        created_in_session_id=session.id,
        validation_criteria="Test task completion is observable.",
    )

    # 4. Claim the task (EventHandlers checks for claimed tasks)
    task_manager.claim_task(task.id, session.id)
    session_var_manager.merge_variables(
        session.id,
        add_claimed_task({}, task.id, f"#{task.seq_num}"),
    )

    # 5. Simulate Edit Tool execution
    # NotebookEdit must use the canonical write-tool tracking path.
    edit_tool = "NotebookEdit"
    assert edit_tool.lower() in EDIT_TOOLS

    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-1",
        source=SessionSource.QWEN,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={"tool_name": edit_tool, "tool_input": {"file_path": str(in_repo_file)}},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    # 6. Verify had_edits is True
    session = session_manager.get(session.id)
    assert session.had_edits
    variables = session_var_manager.get_variables(session.id)
    assert variables["session_edited_files"] == ["src/edited.py"]
    assert variables["task_edited_files"] == {task.id: ["src/edited.py"]}

    # 7. Verify non-edit tool doesn't trigger it (if it was false)
    # Reset session for negative test
    # (Manually unset in DB because we don't have a method to unset it)
    temp_db.execute("UPDATE sessions SET had_edits = FALSE WHERE id = %s", (session.id,))
    session = session_manager.get(session.id)
    assert not session.had_edits

    event_read = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-1",
        source=SessionSource.QWEN,
        timestamp=datetime.now(UTC),
        data={"tool_name": "read_file"},
        metadata={"_platform_session_id": session.id},
    )
    handlers.handle_after_tool(event_read)

    session = session_manager.get(session.id)
    assert not session.had_edits


def test_shell_edit_history_tracks_task_files(temp_db, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)

    project = install_isolated_checkout_project(
        temp_db, repo_root, name="test-shell-edit", machine_id=LOCAL_MACHINE_ID
    ).project
    session = session_manager.register(
        external_id="test-shell-session",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
        title="Shell Edit Session",
    )
    task = task_manager.create_task(
        project_id=project.id,
        title="Shell Edit Task",
        created_in_session_id=session.id,
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(task.id, session.id)
    session_var_manager.merge_variables(
        session.id,
        add_claimed_task({}, task.id, f"#{task.seq_num}"),
    )
    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-shell-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={
            "tool_name": "Bash",
            "tool_input": {"command": "touch src/edited.py docs/edited.md"},
            "canonical_tool_kind": "write",
            "canonical_repo_mutation": True,
            "canonical_file_paths": ["src/edited.py", "docs/edited.md"],
        },
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    variables = session_var_manager.get_variables(session.id)
    assert variables["session_edited_files"] == ["src/edited.py", "docs/edited.md"]
    assert variables["task_edited_files"] == {task.id: ["src/edited.py", "docs/edited.md"]}


def test_edit_history_ignores_out_of_repo_paths(temp_db, tmp_path) -> None:
    """Test claimed out-of-repo edits do not set had_edits or session_edited_files."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_file = tmp_path / "outside" / "settings.json"
    outside_file.parent.mkdir(parents=True)

    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)

    project = install_isolated_checkout_project(
        temp_db, repo_root, name="test-project-outside", machine_id=LOCAL_MACHINE_ID
    ).project
    session = session_manager.register(
        external_id="test-session-outside",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
        title="Out-of-Repo Edit Session",
    )
    task = task_manager.create_task(
        project_id=project.id,
        title="Out-of-Repo Task",
        created_in_session_id=session.id,
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(task.id, session.id)

    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)
    edit_tool = list(EDIT_TOOLS)[0]
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-outside",
        source=SessionSource.QWEN,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={"tool_name": edit_tool, "tool_input": {"file_path": str(outside_file)}},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    session = session_manager.get(session.id)
    assert not session.had_edits
    assert "session_edited_files" not in session_var_manager.get_variables(session.id)


def test_edit_history_not_set_if_task_not_claimed(temp_db) -> None:
    """Test had_edits is NOT set if no task is claimed."""
    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)

    project = project_manager.create("test-project-2")
    project_id = project.id

    session = session_manager.register(
        external_id="test-session-2",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="qwen",
        project_id=project_id,
    )

    # Create task but DON'T claim it
    task_manager.create_task(
        project_id=project_id,
        title="Unclaimed Task",
        created_in_session_id=session.id,
        validation_criteria="Test task completion is observable.",
    )

    edit_tool = list(EDIT_TOOLS)[0]
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-2",
        source=SessionSource.QWEN,
        timestamp=datetime.now(UTC),
        data={"tool_name": edit_tool},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    session = session_manager.get(session.id)
    assert not session.had_edits


def test_edit_history_without_claim_records_no_task_scoped_edits(temp_db, tmp_path) -> None:
    """Unclaimed edits remain session-scoped only."""
    repo_root = tmp_path / "repo-no-claim"
    repo_root.mkdir()
    in_repo_file = repo_root / "src" / "edited.py"
    in_repo_file.parent.mkdir(parents=True)

    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)
    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)

    project = install_isolated_checkout_project(
        temp_db, repo_root, name="test-project-no-claim", machine_id=LOCAL_MACHINE_ID
    ).project
    session = session_manager.register(
        external_id="test-session-no-claim",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="qwen",
        project_id=project.id,
    )

    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-no-claim",
        source=SessionSource.QWEN,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={"tool_name": list(EDIT_TOOLS)[0], "tool_input": {"file_path": str(in_repo_file)}},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    variables = session_var_manager.get_variables(session.id)
    assert variables["session_edited_files"] == ["src/edited.py"]
    assert "task_edited_files" not in variables


def test_edit_history_multiple_claims_use_active_task_id(temp_db, tmp_path) -> None:
    """Multiple claimed tasks attribute edits to active_task_id only."""
    repo_root = tmp_path / "repo-multi-claim"
    repo_root.mkdir()
    in_repo_file = repo_root / "src" / "edited.py"
    in_repo_file.parent.mkdir(parents=True)

    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)
    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)

    project = install_isolated_checkout_project(
        temp_db, repo_root, name="test-project-multi-claim", machine_id=LOCAL_MACHINE_ID
    ).project
    session = session_manager.register(
        external_id="test-session-multi-claim",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="qwen",
        project_id=project.id,
    )
    first = task_manager.create_task(
        project_id=project.id,
        title="First Task",
        created_in_session_id=session.id,
        validation_criteria="Test task completion is observable.",
    )
    second = task_manager.create_task(
        project_id=project.id,
        title="Second Task",
        created_in_session_id=session.id,
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(first.id, session.id)
    task_manager.claim_task(second.id, session.id)
    session_var_manager.merge_variables(
        session.id,
        {
            "active_task_id": second.id,
            "claimed_tasks": {first.id: f"#{first.seq_num}", second.id: f"#{second.seq_num}"},
        },
    )

    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-multi-claim",
        source=SessionSource.QWEN,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={"tool_name": list(EDIT_TOOLS)[0], "tool_input": {"file_path": str(in_repo_file)}},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    variables = session_var_manager.get_variables(session.id)
    assert variables["session_edited_files"] == ["src/edited.py"]
    assert variables["task_edited_files"] == {second.id: ["src/edited.py"]}


def test_codex_patch_ledger_survives_commit_observer_and_compaction_resume(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Reproduce session 9766's missing patch attribution through resume."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project = install_isolated_checkout_project(
        temp_db, repo_root, name="patch-ledger-project", machine_id=LOCAL_MACHINE_ID
    ).project
    variables_manager = SessionVariableManager(temp_db)
    handlers = EventHandlers(
        session_storage=cast(HookSessionManager, session_manager),
        task_manager=task_manager,
    )
    session = session_manager.register(
        external_id="codex-patch-ledger",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    task = task_manager.create_task(
        project_id=project.id,
        title="Attribute Codex patch",
        created_in_session_id=session.id,
        validation_criteria="The exact task edit ledger survives resume.",
    )
    task_manager.claim_task(task.id, session.id)
    variables_manager.merge_variables(
        session.id,
        add_claimed_task({}, task.id, f"#{task.seq_num}"),
    )

    patch_data = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: src/first.py\n"
                "*** Add File: docs/plan.md\n"
                "*** Update File: src/first.py\n"
                "*** End Patch\n"
            )
        },
    }
    normalize_tool_fields(patch_data)
    patch_event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="codex-patch-ledger",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data=patch_data,
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(patch_event)

    expected_task_map = {task.id: ["src/first.py", "docs/plan.md"]}
    variables = variables_manager.get_variables(session.id)
    assert variables["session_edited_files"] == ["src/first.py", "docs/plan.md"]
    assert variables["task_edited_files"] == expected_task_map

    commit_event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="codex-patch-ledger",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'patch ledger'"},
            "tool_output": "[main abc1234] patch ledger\n 2 files changed",
        },
        metadata={"_platform_session_id": session.id},
    )
    detect_bash_commit(commit_event, variables, session.id)
    variables_manager.merge_variables(session.id, variables)
    assert variables_manager.get_variables(session.id)["task_edited_files"] == expected_task_map

    compact_event = HookEvent(
        event_type=HookEventType.PRE_COMPACT,
        session_id="codex-patch-ledger",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"trigger": "manual"},
        metadata={"_platform_session_id": session.id},
    )
    handlers.handle_pre_compact(compact_event)
    resumed = session_manager.register(
        external_id="codex-patch-ledger",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )

    assert resumed.id == session.id
    assert variables_manager.get_variables(session.id)["task_edited_files"] == expected_task_map
