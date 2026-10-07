"""Task call-start bindings survive delayed delivery and daemon object lifetimes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_types import HookSessionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows import task_tool_bindings
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_tool_bindings import TaskToolBindings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "source,name,output,poll_name,poll_input",
    [
        (
            SessionSource.CODEX,
            "exec_command",
            {"session_id": 99},
            "write_stdin",
            {"session_id": 99},
        ),
        (
            SessionSource.CODEX,
            "functions.exec",
            "Script running with cell ID cell-99",
            "functions.wait",
            {"cell_id": "cell-99"},
        ),
        (
            SessionSource.CLAUDE,
            "Bash",
            "Command did not complete within its 10s timeout and was moved to the background (ID: job-99). Output is being written to: /tmp/job-99.output. ",
            "TaskOutput",
            {"task_id": "job-99"},
        ),
    ],
)
def test_background_poll_uses_persisted_original_start_and_releases_fence(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    source: SessionSource,
    name: str,
    output: object,
    poll_name: str,
    poll_input: dict[str, object],
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Poll retains start."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Poll releases fence."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    epoch = variables.get_variables(canonical_task_session.id)["task_selection_history"][-1][
        "epoch"
    ]
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=source,
        session_id=canonical_task_session.external_id,
        timestamp=datetime.fromisoformat(epoch),
        request_id="background-start",
        data={
            "tool_name": name,
            "tool_input": {"command": "uv run pytest tests/unit"},
            "arguments": 'await tools.exec_command({cmd: "uv run pytest tests/unit"})',
        },
    )
    binding = TaskToolBindings(variables, canonical_task_session.id)
    binding.start(before)
    binding.complete(
        replace(
            before, event_type=HookEventType.AFTER_TOOL, data={**before.data, "tool_output": output}
        )
    )
    binding.clear_pending("turn_end")
    with pytest.raises(ValueError, match="background-start"):
        tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    poll = replace(
        before,
        timestamp=before.timestamp + timedelta(seconds=10),
        request_id="poll-result",
        data={"tool_name": poll_name, "tool_input": poll_input, "arguments": poll_input},
    )
    recreated = TaskToolBindings(SessionVariableManager(temp_db), canonical_task_session.id)
    recreated.start(poll)
    recreated.complete(
        replace(
            poll,
            event_type=HookEventType.AFTER_TOOL,
            data={**poll.data, "tool_output": "receipt unavailable"},
        )
    )
    with pytest.raises(ValueError, match="background-start"):
        tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    after = replace(
        poll,
        event_type=HookEventType.AFTER_TOOL,
        data={**poll.data, "tool_output": {"exit_code": 0, "output": "passed"}},
    )
    assert recreated.started_at(after) == before.timestamp.timestamp()
    recreated.complete(after)
    assert (
        tasks.claim_task_for_agent(second.id, canonical_task_session.id).claimed_by_session_id
        == canonical_task_session.id
    )
    assert recreated.started_at(after) == before.timestamp.timestamp()


def test_completed_call_replay_retains_its_start_after_focus_changes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Replay retains original selection."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Replay retains original selection."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    epoch = variables.get_variables(canonical_task_session.id)["task_selection_history"][-1][
        "epoch"
    ]
    start = datetime.fromisoformat(epoch)
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        timestamp=start,
        request_id="edit-A",
        data={"tool_name": "Edit", "canonical_repo_mutation": True},
    )
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    bindings.start(before)
    after = replace(before, event_type=HookEventType.AFTER_TOOL)
    bindings.complete(after)
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)

    # A fresh object stands in for a daemon restart/outage replay.
    replay = TaskToolBindings(SessionVariableManager(temp_db), canonical_task_session.id)
    started_at = replay.started_at(after)
    assert started_at == start.timestamp()
    assert variables.record_edited_files(
        canonical_task_session.id, ["src/a.py"], started_at=started_at
    )
    state = variables.get_variables(canonical_task_session.id)
    assert state["active_task_id"] == second.id
    assert state["task_edited_files"] == {first.id: ["src/a.py"]}


@pytest.mark.parametrize("boundary", ["interrupt", "turn_end", "session_end", "clear"])
def test_boundary_cleanup_preserves_replay_and_releases_lost_calls(
    temp_db: HubDatabase,
    canonical_task_session: Session,
    boundary: str,
) -> None:
    variables = SessionVariableManager(temp_db)
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        request_id="sync-call",
        timestamp=datetime(2026, 10, 7, tzinfo=UTC),
        data={"tool_name": "Bash"},
    )
    background = replace(before, request_id="background-call")
    bindings.start(before)
    bindings.start(background)
    bindings.complete(
        replace(
            background,
            event_type=HookEventType.AFTER_TOOL,
            data={"_verification_pending": True},
        )
    )

    bindings.clear_pending(boundary)

    calls = variables.get_variables(canonical_task_session.id)["task_tool_bindings"]
    assert calls["codex:sync-call"]["pending"] is False
    assert calls["codex:background-call"]["pending"] is (boundary in {"interrupt", "turn_end"})
    assert bindings.started_at(before) == before.timestamp.timestamp()
    assert bindings.started_at(background) == background.timestamp.timestamp()


@pytest.mark.parametrize(
    "event_type", [HookEventType.STOP, HookEventType.INTERRUPT, HookEventType.SESSION_END]
)
def test_pre_rule_cleanup_updates_persisted_and_evaluation_state(
    temp_db: HubDatabase,
    canonical_task_session: Session,
    event_type: HookEventType,
) -> None:
    variables = SessionVariableManager(temp_db)
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        request_id="abandoned-call",
        timestamp=datetime(2026, 10, 7, tzinfo=UTC),
        data={"tool_name": "Bash"},
    )
    bindings.start(before)
    evaluation = variables.get_variables(canonical_task_session.id)
    task_tool_bindings.cleanup_task_tool_bindings(
        replace(before, event_type=event_type),
        variables,
        canonical_task_session.id,
        evaluation,
    )
    assert evaluation["task_tool_bindings"]["codex:abandoned-call"]["pending"] is False
    assert (
        variables.get_variables(canonical_task_session.id)["task_tool_bindings"]
        == evaluation["task_tool_bindings"]
    )


def test_force_steal_during_native_agent_refuses_unbound_child_edit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Transferred agent edits cannot drift."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Transferred agent edits cannot drift."
    )
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    epoch = variables.get_variables(canonical_task_session.id)["task_selection_history"][-1][
        "epoch"
    ]
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        timestamp=datetime.fromisoformat(epoch),
        request_id="native-agent",
        data={"tool_name": "Task"},
    )
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    bindings.start(before)
    receiver = SessionManager(temp_db).register_session(
        external_id="force-steal-receiver",
        machine_id=canonical_task_session.machine_id,
        source="codex",
        project_id=sample_project["id"],
    )
    assert receiver
    tasks.claim_task(first.id, receiver, force=True)
    assert variables.get_variables(canonical_task_session.id)["active_task_id"] == second.id
    child = replace(
        before,
        timestamp=datetime.now(UTC),
        request_id="unbound-child-edit",
        data={"tool_name": "Edit"},
        metadata={"_native_subagent_binding": True},
    )
    with pytest.raises(ValueError, match="native-agent.*ownership.*claim_task"):
        bindings.start(child)
    assert bindings.started_at(replace(child, event_type=HookEventType.AFTER_TOOL)) is None
    assert variables.get_variables(canonical_task_session.id).get("task_edited_files", {}) == {}
    assert (
        variables.record_edited_files(
            canonical_task_session.id,
            ["stale-child.py"],
            checkout_root="/checkout",
            started_at=before.timestamp.timestamp(),
        )
        is False
    )
    assert variables.get_variables(canonical_task_session.id).get("session_edited_files", []) == []


def test_failed_yielded_call_releases_pending_binding(
    temp_db: HubDatabase,
    canonical_task_session: Session,
) -> None:
    variables = SessionVariableManager(temp_db)
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        request_id="failed-call",
        timestamp=datetime(2026, 10, 7, tzinfo=UTC),
        data={"tool_name": "Bash"},
    )
    bindings.start(before)
    bindings.complete(
        replace(
            before,
            event_type=HookEventType.AFTER_TOOL,
            data={"_verification_pending": True},
            metadata={"is_failure": True},
        )
    )
    calls = variables.get_variables(canonical_task_session.id)["task_tool_bindings"]
    assert calls["codex:failed-call"]["pending"] is False
    assert bindings.started_at(before) == before.timestamp.timestamp()


def test_own_switch_waits_for_the_selected_tasks_running_call(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Running calls fence own switches."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Running calls fence own switches."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    epoch = variables.get_variables(canonical_task_session.id)["task_selection_history"][-1][
        "epoch"
    ]
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        request_id="running-call",
        timestamp=datetime.fromisoformat(epoch),
        data={"tool_name": "Bash"},
    )
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    bindings.start(before)

    with pytest.raises(ValueError, match="running-call.*wait.*stop"):
        tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    count = tasks.count_tasks(project_id=sample_project["id"])
    with pytest.raises(ValueError, match="running-call.*wait.*stop"):
        tasks.create_task_for_agent(
            canonical_task_session.id,
            project_id=sample_project["id"],
            title="Must roll back",
            validation_criteria="A fenced create-and-claim creates no task.",
        )
    assert tasks.count_tasks(project_id=sample_project["id"]) == count

    assert tasks.get_task(second.id).claimed_by_session_id is None
    assert variables.get_variables(canonical_task_session.id)["active_task_id"] == first.id
    bindings.complete(replace(before, event_type=HookEventType.AFTER_TOOL))
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    assert variables.get_variables(canonical_task_session.id)["active_task_id"] == second.id


def test_anonymous_completion_requires_unchanged_selection(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Anonymous calls cannot guess selection."
    )
    second = tasks.create_task(
        sample_project["id"],
        "Second",
        validation_criteria="Anonymous calls cannot guess selection.",
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    epoch = variables.get_variables(canonical_task_session.id)["task_selection_history"][-1][
        "epoch"
    ]
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        timestamp=datetime.fromisoformat(epoch),
        data={"tool_name": "Edit"},
    )
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    bindings.start(before)
    after = replace(
        before,
        event_type=HookEventType.AFTER_TOOL,
        timestamp=before.timestamp + timedelta(seconds=1),
    )
    assert bindings.started_at(after) == before.timestamp.timestamp()

    # Canonical transfers can change selection while a call is running.
    tasks.claim_task(second.id, canonical_task_session.id)
    assert bindings.started_at(after) is None
    tasks.claim_task(first.id, canonical_task_session.id)
    assert bindings.started_at(after) is None


@pytest.mark.parametrize("has_start", [True, False])
def test_tool_hooks_credit_only_edits_with_a_bound_start(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_start: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Hooks bind task selection at start."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Hooks bind task selection at start."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    epoch = variables.get_variables(canonical_task_session.id)["task_selection_history"][-1][
        "epoch"
    ]
    handlers = EventHandlers(
        session_manager=cast(HookSessionManager, SessionManager(temp_db)), task_manager=tasks
    )

    def resolve_path(
        file_path: str, cwd: str | None, *, project_id: str | None = None
    ) -> tuple[Path, str]:
        assert file_path == "src/bound.py"
        return tmp_path, file_path

    monkeypatch.setattr(handlers, "_resolve_repo_edit_paths", resolve_path)
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CODEX,
        session_id=canonical_task_session.external_id,
        project_id=sample_project["id"],
        timestamp=datetime.fromisoformat(epoch),
        request_id="delayed-edit",
        data={"tool_name": "Edit", "tool_input": {"file_path": "src/bound.py"}},
        metadata={"_platform_session_id": canonical_task_session.id},
    )
    if has_start:
        assert handlers.handle_before_tool(before).decision == "allow"
    tasks.claim_task(second.id, canonical_task_session.id)
    after = replace(before, event_type=HookEventType.AFTER_TOOL, timestamp=datetime.now(UTC))
    assert handlers.handle_after_tool(after).decision == "allow"
    state = variables.get_variables(canonical_task_session.id)
    expected = {first.id: ["src/bound.py"]} if has_start else {}
    assert state.get("task_edited_files", {}) == expected
    assert state["active_task_id"] == second.id
    if has_start:
        assert state["task_tool_bindings"]["codex:delayed-edit"]["pending"] is False
        conflict = replace(before, timestamp=datetime.now(UTC), request_id="other-task-edit")
        response = handlers.handle_before_tool(conflict)
        assert response.decision == "block"
        assert response.reason is not None
        assert f"#{first.seq_num}" in response.reason
        assert "src/bound.py" in response.reason
        assert variables.get_variables(canonical_task_session.id)["task_edited_files"] == expected
    else:
        assert "proven tool-start task binding" in caplog.text
        assert "claim_task" in caplog.text
