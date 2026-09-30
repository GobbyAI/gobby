"""Canonical task transfers release the former owner's session claim state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.observers import reconcile_claimed_tasks
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture
def claimed_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, str]:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    sessions = SessionManager(temp_db)
    owner, replacement = (
        sessions.register(
            external_id=name,
            machine_id=MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        for name in ("transfer-owner", "transfer-replacement")
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Reviewer transfer",
        validation_criteria="The prior owner can hand off after transfer.",
    )
    claim_task(temp_db, task.id, owner.id)
    SessionVariableManager(temp_db).merge_variables(
        owner.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "task_claimed": True,
            "active_task_id": task.id,
            "task_has_commits": True,
            "task_edited_files": {task.id: ["src/prior.py"]},
            "session_dirty_files": ["src/prior.py"],
        },
    )
    return owner.id, replacement.id, task.id


def _claim_gates_block(variables: dict[str, Any]) -> tuple[bool, bool]:
    rules_root = Path(__file__).resolve().parents[3] / "src/gobby/install/shared/workflows/rules"
    evaluator = SafeExpressionEvaluator(
        {"variables": variables, "tool_input": {"clear_session": True}},
        {"bool": bool, "has_durable_stop_wait": lambda: False},
    )
    clear_rules = yaml.safe_load(
        (rules_root / "context-handoff/require-handoff-discipline.yaml").read_text()
    )
    stop_rules = yaml.safe_load((rules_root / "stop-gates/require-task-close.yaml").read_text())
    return (
        evaluator.evaluate(clear_rules["rules"]["block-clear-session-with-claimed-tasks"]["when"]),
        evaluator.evaluate(stop_rules["rules"]["require-task-close"]["when"]),
    )


@pytest.mark.parametrize("force", [True, False], ids=["force", "expected-owner"])
@pytest.mark.parametrize("retain_other_claim", [False, True], ids=["last-claim", "other-claim"])
def test_transfer_releases_only_the_prior_owners_transferred_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    claimed_state: tuple[str, str, str],
    force: bool,
    retain_other_claim: bool,
) -> None:
    owner, replacement, task_id = claimed_state
    variables = SessionVariableManager(temp_db)
    tasks = LocalTaskManager(temp_db)
    remaining: dict[str, str] = {}
    if retain_other_claim:
        other = tasks.create_task(
            sample_project["id"],
            title="Prior unrelated claim",
            validation_criteria="Preserve this unrelated claim during transfer.",
            claimed_by_session_id=owner,
        )
        remaining[other.id] = f"#{other.seq_num}"
        state = variables.get_variables(owner)
        variables.merge_variables(owner, {"claimed_tasks": {**state["claimed_tasks"], **remaining}})

    assert _claim_gates_block(variables.get_variables(owner)) == (True, True)
    claim_task(
        temp_db,
        task_id,
        replacement,
        force=force,
        expected_owner=None if force else owner,
    )
    released = variables.get_variables(owner)

    assert tasks.get_task(task_id).claimed_by_session_id == replacement
    assert released["claimed_tasks"] == remaining
    assert released["task_claimed"] is retain_other_claim
    assert released["active_task_id"] == next(iter(remaining), None)
    assert released["task_edited_files"] == {task_id: ["src/prior.py"]}
    assert released["session_dirty_files"] == ["src/prior.py"]
    assert _claim_gates_block(released) == (retain_other_claim, retain_other_claim)


@pytest.mark.parametrize("retain_other_claim", [False, True], ids=["empty", "other-claim"])
def test_reconciliation_repairs_an_active_id_left_by_an_earlier_transfer(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    claimed_state: tuple[str, str, str],
    retain_other_claim: bool,
) -> None:
    owner, replacement, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    claim_task(temp_db, task_id, replacement, force=True)
    state: dict[str, Any] = {"claimed_tasks": {}, "task_claimed": False, "active_task_id": task_id}
    expected: str | None = None
    if retain_other_claim:
        other = tasks.create_task(
            sample_project["id"],
            title="Unrelated retained work",
            validation_criteria="Retain the legitimate active task.",
            claimed_by_session_id=owner,
        )
        state["claimed_tasks"] = {other.id: f"#{other.seq_num}"}
        expected = other.id

    reconcile_claimed_tasks(state, owner, task_manager=tasks)

    assert state["active_task_id"] == expected
    assert state["task_claimed"] is retain_other_claim


def test_failed_variable_cleanup_rolls_back_canonical_transfer(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, replacement, task_id = claimed_state

    def fail_cleanup(self: SessionVariableManager, session_id: str, task_id: str) -> bool:
        raise RuntimeError("variable cleanup failed")

    monkeypatch.setattr(SessionVariableManager, "release_task_claim", fail_cleanup)
    with pytest.raises(RuntimeError, match="variable cleanup failed"):
        claim_task(temp_db, task_id, replacement, force=True)

    assert LocalTaskManager(temp_db).get_task(task_id).claimed_by_session_id == owner
    assert SessionVariableManager(temp_db).get_variables(owner)["active_task_id"] == task_id


@pytest.mark.asyncio
async def test_a_late_mcp_claim_result_does_not_restore_a_transferred_claim(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, replacement, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)

    def transfer_before_variable_write(
        self: SessionTaskManager, session_id: str, task_id: str, action: str
    ) -> None:
        claim_task(temp_db, task_id, owner, force=True)

    monkeypatch.setattr(SessionTaskManager, "link_task", transfer_before_variable_write)
    registry = create_task_registry(tasks)
    with session_context_for_test(replacement):
        result = await registry.call("claim_task", {"task_id": task_id, "force": True})

    assert result.get("success") is True
    assert tasks.get_task(task_id).claimed_by_session_id == owner
    state = SessionVariableManager(temp_db).get_variables(replacement)
    assert state["claimed_tasks"] == {}
    assert state["task_claimed"] is False
    assert state["active_task_id"] is None
