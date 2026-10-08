"""Canonical task transfers release the former owner's session claim state."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from psycopg.errors import LockNotAvailable

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.event_handlers._session_responses import get_claimed_task_info
from gobby.hooks.event_handlers._session_start.claims import preserve_task_claim_state
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows import state_manager as variable_state
from gobby.workflows.engine import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
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
        variables.merge_variables(
            owner,
            {
                "claimed_tasks": {**state["claimed_tasks"], **remaining},
                "active_task_id": task_id,
            },
        )

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
    # A retained claim receives edits again only through a reclaim (#23665).
    assert released["active_task_id"] is None
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
    if retain_other_claim:
        other = tasks.create_task(
            sample_project["id"],
            title="Unrelated retained work",
            validation_criteria="Retain the claim the transfer left behind.",
            claimed_by_session_id=owner,
        )
        state["claimed_tasks"] = {other.id: f"#{other.seq_num}"}

    reconcile_claimed_tasks(state, owner, task_manager=tasks)

    # The retained claim stays claimed and receives edits only after a reclaim (#23665).
    assert state["active_task_id"] is None
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transfer_at", ["before_observer", "before_persistence", "no_transfer", "locked_persistence"]
)
@pytest.mark.parametrize("keep_other_claim", [False, True])
async def test_after_tool_claim_observation_cannot_restore_transferred_ownership(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transfer_at: str,
    keep_other_claim: bool,
) -> None:
    owner, caller, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(task_id)
    variables = SessionVariableManager(temp_db)
    other_claims: dict[str, str] = {}
    if keep_other_claim:
        other = tasks.create_task(
            task.project_id,
            title="Unrelated owned claim",
            validation_criteria="The unrelated claim remains owned after another task transfers.",
        )
        claim_task(temp_db, other.id, caller)
        other_claims[other.id] = f"#{other.seq_num}"
    variables.merge_variables(
        caller,
        {
            "_variable_defaults_loaded": True,
            "claimed_tasks": other_claims,
            "task_claimed": bool(other_claims),
            "active_task_id": next(iter(other_claims), None),
            "task_edited_files": {task_id: ["src/caller.py"]},
            "unrelated_marker": "preserved",
        },
    )
    # The canonical claim completes before the AFTER_TOOL snapshot is observed.
    # The observer must safely hydrate a claim whose MCP variable write is late.
    claim_task(temp_db, task_id, caller, force=True)
    if transfer_at == "before_observer":
        claim_task(temp_db, task_id, owner, force=True)

    row_probe_seen = False
    if transfer_at == "locked_persistence":
        original_encode = variable_state._encode_variables_payload

        def another_connection_cannot_lock_claim(holder_pid: int) -> bool:
            try:
                with temp_db.transaction() as conn:
                    probe = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
                    assert probe is not None and probe["pid"] != holder_pid
                    conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE NOWAIT", (task_id,))
            except LockNotAvailable:
                return True
            return False

        def encode_with_locked_claim(payload: dict[str, Any]) -> str:
            nonlocal row_probe_seen
            if task_id in payload.get("claimed_tasks", {}):
                row_probe_seen = True
                holder = temp_db.fetchone("SELECT pg_backend_pid() AS pid")
                assert holder is not None
                with ThreadPoolExecutor(max_workers=1) as pool:
                    blocked = pool.submit(
                        another_connection_cannot_lock_claim, holder["pid"]
                    ).result(timeout=5)
                assert blocked, "canonical owner can change while the hook persists its claim"
            return original_encode(payload)

        monkeypatch.setattr(variable_state, "_encode_variables_payload", encode_with_locked_claim)

    observed: dict[str, Any] = {}

    async def evaluate(**kwargs: Any) -> HookResponse:
        observed.update(kwargs["variables"])
        if transfer_at == "before_persistence":
            claim_task(temp_db, task_id, owner, force=True)
        return HookResponse(decision="allow")

    engine = MagicMock(spec=RuleEngine, db=temp_db)
    engine.evaluate = AsyncMock(side_effect=evaluate)
    handler = WorkflowHookHandler(
        rule_engine=cast(RuleEngine, engine),
        task_manager=tasks,
        session_manager=SessionManager(temp_db),
        session_task_manager=SessionTaskManager(temp_db),
    )
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="external-caller",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=task.project_id,
        cwd=str(tmp_path),
        metadata={"_platform_session_id": caller},
        data={
            "mcp_server": "gobby-tasks",
            "mcp_tool": "claim_task",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": task_id, "force": True},
            },
            "tool_output": {"success": True, "result": {"id": task_id}},
        },
    )
    response = await handler.evaluate_async(event)
    assert response.decision == "allow"
    assert engine.evaluate.await_count == 1
    assert (task_id in observed["claimed_tasks"]) is (transfer_at != "before_observer")

    expected_claims = dict(other_claims)
    if transfer_at in ("no_transfer", "locked_persistence"):
        expected_claims[task_id] = f"#{task.seq_num}"
    state = variables.get_variables(caller)
    assert state["claimed_tasks"] == expected_claims
    assert state["task_claimed"] is bool(expected_claims)
    assert state["active_task_id"] == (
        task_id if transfer_at in ("no_transfer", "locked_persistence") else None
    )
    assert _claim_gates_block(state) == (bool(expected_claims), bool(expected_claims))
    assert state["task_edited_files"] == {task_id: ["src/caller.py"]}
    assert state["unrelated_marker"] == "preserved"
    if transfer_at == "locked_persistence":
        assert row_probe_seen
    expected_owner = caller if transfer_at in ("no_transfer", "locked_persistence") else owner
    assert tasks.get_task(task_id).claimed_by_session_id == expected_owner


def _session_response_handler(db: HubDatabase, tasks: LocalTaskManager) -> EventHandlersBase:
    return cast(
        EventHandlersBase,
        SimpleNamespace(
            _session_manager=SessionManager(db),
            _task_manager=tasks,
            _session_task_manager=SessionTaskManager(db),
        ),
    )


def _prepare_projection(
    db: HubDatabase,
    owner: str,
    task_id: str,
    mode: str,
) -> None:
    task = LocalTaskManager(db).get_task(task_id)
    claims = {} if mode == "fallback" else {task_id: "#old-ref"}
    if mode == "prune":
        claims["21000000-0000-4000-8000-000000000099"] = "#missing"
    SessionVariableManager(db).merge_variables(
        owner,
        {
            "_variable_defaults_loaded": True,
            "claimed_tasks": claims,
            "task_claimed": bool(claims),
            "active_task_id": task.id if claims else None,
            "unrelated_marker": "preserved",
        },
    )


@pytest.mark.parametrize("mode", ["fallback", "ref_refresh", "prune"])
@pytest.mark.parametrize("scenario", ["transfer", "transfer_with_new_claim", "escalated_owned"])
def test_session_response_projection_uses_current_ownership(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    scenario: str,
) -> None:
    owner, replacement, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(task_id)
    variables = SessionVariableManager(temp_db)
    _prepare_projection(temp_db, owner, task_id, mode)
    expected: dict[str, str] = {}
    if scenario == "escalated_owned":
        tasks.escalate_task(task_id, reason="An operator owns the close review.")
        claim_task(temp_db, task_id, owner)
        expected[task_id] = f"#{task.seq_num}"
    other = tasks.create_task(
        task.project_id,
        title="Fresh unrelated projection claim",
        validation_criteria="The current claim survives a stale projection write.",
        additional_skills=["python"],
    )
    snapshot_seen = False

    def after_snapshot() -> None:
        nonlocal snapshot_seen
        if snapshot_seen:
            return
        snapshot_seen = True
        if scenario == "escalated_owned":
            return
        claim_task(temp_db, task_id, replacement, force=True)
        if scenario == "transfer_with_new_claim":
            claim_task(temp_db, other.id, owner)
            expected[other.id] = f"#{other.seq_num}"
            variables.merge_variables(
                owner,
                {"claimed_tasks": dict(expected), "task_claimed": True, "active_task_id": other.id},
            )

    original_list = tasks.list_tasks
    original_get = tasks.get_task

    def list_snapshot(**kwargs: Any) -> list[Any]:
        snapshot = original_list(**kwargs)
        after_snapshot()
        return snapshot

    def get_snapshot(lookup_id: str, **kwargs: Any) -> Any:
        snapshot = original_get(lookup_id, **kwargs)
        if lookup_id == task_id:
            after_snapshot()
        return snapshot

    monkeypatch.setattr(tasks, "list_tasks", list_snapshot)
    monkeypatch.setattr(tasks, "get_task", get_snapshot)
    result = get_claimed_task_info(
        _session_response_handler(temp_db, tasks), owner, task.project_id
    )
    assert snapshot_seen
    state = variables.get_variables(owner)
    assert state["claimed_tasks"] == expected
    assert state["task_claimed"] is bool(expected)
    assert (
        state.get("active_task_id") in expected if expected else state.get("active_task_id") is None
    )
    assert state["unrelated_marker"] == "preserved"
    assert state["task_edited_files"] == {task_id: ["src/prior.py"]}
    expected_owner = owner if scenario == "escalated_owned" else replacement
    assert original_get(task_id).claimed_by_session_id == expected_owner
    if scenario == "escalated_owned":
        assert result == [(f"#{task.seq_num}", "escalated", task.title)]


@pytest.mark.parametrize("mode", ["fallback", "prune"])
@pytest.mark.parametrize("event_type", [HookEventType.SESSION_START, HookEventType.STOP])
@pytest.mark.parametrize("scenario", ["transfer", "transfer_with_new_claim", "escalated_owned"])
@pytest.mark.asyncio
async def test_hook_hydration_projection_uses_current_ownership(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    tmp_path: Path,
    mode: str,
    event_type: HookEventType,
    scenario: str,
) -> None:
    owner, replacement, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(task_id)
    variables = SessionVariableManager(temp_db)
    _prepare_projection(temp_db, owner, task_id, mode)
    expected: dict[str, str] = {}
    if scenario == "escalated_owned":
        tasks.escalate_task(task_id, reason="An operator owns the close review.")
        claim_task(temp_db, task_id, owner)
        expected[task_id] = f"#{task.seq_num}"
    other = tasks.create_task(
        task.project_id,
        title="Claim created after hydration",
        validation_criteria="Hydration preserves a newly claimed unrelated task.",
        additional_skills=["python"],
    )
    observed: dict[str, Any] = {}

    async def evaluate(**kwargs: Any) -> HookResponse:
        observed.update(kwargs["variables"])
        if scenario != "escalated_owned":
            claim_task(temp_db, task_id, replacement, force=True)
        if scenario == "transfer_with_new_claim":
            claim_task(temp_db, other.id, owner)
            expected[other.id] = f"#{other.seq_num}"
            variables.merge_variables(
                owner,
                {"claimed_tasks": dict(expected), "task_claimed": True, "active_task_id": other.id},
            )
        return HookResponse(decision="allow")

    engine = MagicMock(spec=RuleEngine, db=temp_db)
    engine.evaluate = AsyncMock(side_effect=evaluate)
    handler = WorkflowHookHandler(
        rule_engine=cast(RuleEngine, engine),
        task_manager=tasks,
        session_manager=SessionManager(temp_db),
        session_task_manager=SessionTaskManager(temp_db),
    )
    event = HookEvent(
        event_type=event_type,
        session_id="external-hydration-owner",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=task.project_id,
        cwd=str(tmp_path),
        metadata={"_platform_session_id": owner},
        data={},
    )
    response = await handler.evaluate_async(event)
    assert response.decision == "allow"
    assert engine.evaluate.await_count == 1
    if scenario != "escalated_owned":
        assert task_id in observed["claimed_tasks"]
    state = variables.get_variables(owner)
    assert state["claimed_tasks"] == expected
    assert state["task_claimed"] is bool(expected)
    assert (
        state.get("active_task_id") in expected if expected else state.get("active_task_id") is None
    )
    assert state["claimed_task_extra_skills"] == (
        ["python"] if scenario == "transfer_with_new_claim" else []
    )
    assert state["unrelated_marker"] == "preserved"
    assert state["task_edited_files"] == {task_id: ["src/prior.py"]}
    assert tasks.get_task(task_id).claimed_by_session_id == (
        owner if scenario == "escalated_owned" else replacement
    )


@pytest.mark.parametrize("scenario", ["transfer", "transfer_with_new_claim", "no_transfer"])
def test_successor_projection_cannot_restore_a_third_owners_claim(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    predecessor, successor, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(task_id)
    sessions = SessionManager(temp_db)
    third = sessions.register(
        external_id="third-projection-owner",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=task.project_id,
    )
    links = SessionTaskManager(temp_db)
    handler = _session_response_handler(temp_db, tasks)
    variables = SessionVariableManager(temp_db)
    predecessor_vars = variables.get_variables(predecessor)
    other = tasks.create_task(
        task.project_id,
        title="Fresh successor claim",
        validation_criteria="Successor hydration preserves a fresh unrelated task.",
    )
    expected: dict[str, str] = {}
    before_merge = variables.merge_variables
    merge_seen = False

    def transfer_before_merge(session_id: str, updates: dict[str, Any], **kwargs: Any) -> bool:
        nonlocal merge_seen
        merge_seen = True
        assert session_id == successor
        assert tasks.get_task(task_id).claimed_by_session_id == successor
        assert any(
            item["task"].id == task_id and item["action"] == "claimed"
            for item in links.get_session_tasks(successor)
        )
        if scenario == "no_transfer":
            expected[task_id] = f"#{task.seq_num}"
        else:
            claim_task(temp_db, task_id, third.id, expected_owner=successor)
        if scenario == "transfer_with_new_claim":
            claim_task(temp_db, other.id, successor)
            expected[other.id] = f"#{other.seq_num}"
            before_merge(
                successor,
                {"claimed_tasks": dict(expected), "task_claimed": True, "active_task_id": other.id},
            )
        return before_merge(session_id, updates, **kwargs)

    monkeypatch.setattr(variables, "merge_variables", transfer_before_merge)
    preserve_task_claim_state(handler, variables, successor, predecessor, predecessor_vars)
    assert merge_seen
    state = variables.get_variables(successor)
    assert state["claimed_tasks"] == expected
    assert state["task_claimed"] is bool(expected)
    if scenario != "no_transfer":
        assert (
            state.get("active_task_id") in expected
            if expected
            else state.get("active_task_id") is None
        )
    assert tasks.get_task(task_id).claimed_by_session_id == (
        successor if scenario == "no_transfer" else third.id
    )
    assert variables.get_variables(predecessor)["claimed_tasks"] == {}
    assert any(
        item["task"].id == task_id and item["action"] == "claimed"
        for item in links.get_session_tasks(successor)
    )


@pytest.mark.parametrize("writer", ["session_response", "hook_hydration", "successor"])
@pytest.mark.asyncio
async def test_projection_locks_all_claim_rows_before_variables_and_through_write(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    predecessor, successor, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(task_id)
    other = tasks.create_task(
        task.project_id,
        title="Second row in projection",
        validation_criteria="Every projected claim is fenced through persistence.",
    )
    claim_task(temp_db, other.id, predecessor)
    expected = {task_id: f"#{task.seq_num}", other.id: f"#{other.seq_num}"}
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(predecessor, {"claimed_tasks": expected, "task_claimed": True})
    predecessor_vars = variables.get_variables(predecessor)
    owner = successor if writer == "successor" else predecessor
    if writer != "successor":
        _prepare_projection(temp_db, owner, task_id, "fallback")
    probes: list[tuple[str, dict[str, bool]]] = []

    def row_is_locked(claim_id: str, holder_pid: int) -> bool:
        try:
            with temp_db.transaction() as conn:
                backend = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
                assert backend is not None and backend["pid"] != holder_pid
                conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE NOWAIT", (claim_id,))
        except LockNotAvailable:
            return True
        return False

    def probe_rows(phase: str) -> None:
        # Keep this backend checked out while the other thread obtains its own.
        # The harness holds no task locks; only the real writer may fence rows.
        with temp_db.transaction() as conn:
            holder = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
            assert holder is not None
            with ThreadPoolExecutor(max_workers=1) as pool:
                locked = {
                    claim_id: pool.submit(row_is_locked, claim_id, holder["pid"]).result(timeout=5)
                    for claim_id in sorted(expected)
                }
        probes.append((phase, locked))

    original_mutate = SessionVariableManager._mutate_variables
    original_encode = variable_state._encode_variables_payload

    def mutate_after_rows(
        self: SessionVariableManager,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[Any, bool]],
        **kwargs: Any,
    ) -> Any:
        if session_id == owner:
            probe_rows("before_variable_lock")
        return original_mutate(self, session_id, mutator, **kwargs)

    def encode_while_rows_are_locked(payload: dict[str, Any]) -> str:
        if payload.get("claimed_tasks") == expected:
            probe_rows("final_payload")
        return original_encode(payload)

    monkeypatch.setattr(SessionVariableManager, "_mutate_variables", mutate_after_rows)
    monkeypatch.setattr(variable_state, "_encode_variables_payload", encode_while_rows_are_locked)
    if writer == "session_response":
        result = get_claimed_task_info(
            _session_response_handler(temp_db, tasks), owner, task.project_id
        )
        assert result is not None and len(result) == 2
    elif writer == "successor":
        preserve_task_claim_state(
            _session_response_handler(temp_db, tasks),
            variables,
            successor,
            predecessor,
            predecessor_vars,
        )
    else:
        engine = MagicMock(spec=RuleEngine, db=temp_db)
        engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        handler = WorkflowHookHandler(
            rule_engine=cast(RuleEngine, engine),
            task_manager=tasks,
            session_manager=SessionManager(temp_db),
            session_task_manager=SessionTaskManager(temp_db),
        )
        response = await handler.evaluate_async(
            HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id="external-row-probe",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                project_id=task.project_id,
                cwd=str(tmp_path),
                metadata={"_platform_session_id": owner},
                data={},
            )
        )
        assert response.decision == "allow"
    assert variables.get_variables(owner)["claimed_tasks"] == expected
    assert {phase for phase, _ in probes} == {"before_variable_lock", "final_payload"}
    assert all(all(locked.values()) for _, locked in probes), probes


@pytest.mark.parametrize("writer", ["session_response", "hook_hydration", "successor"])
@pytest.mark.asyncio
async def test_projection_retries_new_claims_without_variable_first_row_locks(
    temp_db: HubDatabase,
    claimed_state: tuple[str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    predecessor, successor, task_id = claimed_state
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(task_id)
    other = tasks.create_task(
        task.project_id,
        title="Claim arriving after projection row locks",
        validation_criteria="A newer claim and active attribution survive projection retry.",
        additional_skills=["python"],
    )
    variables = SessionVariableManager(temp_db)
    predecessor_vars = variables.get_variables(predecessor)
    owner = successor if writer == "successor" else predecessor
    if writer != "successor":
        _prepare_projection(temp_db, owner, task_id, "fallback")
    guard_thread: int | None = None
    guard_attempts = 0
    injected = False
    original_mutate = SessionVariableManager._mutate_variables

    def add_fresh_claim() -> None:
        claim_task(temp_db, other.id, owner)
        variables.merge_variables(
            owner,
            {
                "claimed_tasks": {other.id: f"#{other.seq_num}"},
                "task_claimed": True,
                "active_task_id": other.id,
                "unrelated_marker": "fresh",
            },
        )

    def mutate_after_fresh_claim(
        self: SessionVariableManager,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[Any, bool]],
        **kwargs: Any,
    ) -> Any:
        nonlocal guard_thread, guard_attempts, injected
        if session_id == owner:
            if guard_thread is None:
                guard_thread = get_ident()
            if guard_thread == get_ident():
                guard_attempts += 1
            if not injected:
                injected = True
                # The real storage claim and projection commit before this
                # writer takes its variable lock. No sleep or product row lock.
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(add_fresh_claim).result(timeout=5)
        return original_mutate(self, session_id, mutator, **kwargs)

    monkeypatch.setattr(SessionVariableManager, "_mutate_variables", mutate_after_fresh_claim)
    if writer == "session_response":
        get_claimed_task_info(_session_response_handler(temp_db, tasks), owner, task.project_id)
    elif writer == "successor":
        preserve_task_claim_state(
            _session_response_handler(temp_db, tasks),
            variables,
            successor,
            predecessor,
            predecessor_vars,
        )
    else:
        engine = MagicMock(spec=RuleEngine, db=temp_db)
        engine.evaluate = AsyncMock(return_value=HookResponse(decision="allow"))
        handler = WorkflowHookHandler(
            rule_engine=cast(RuleEngine, engine),
            task_manager=tasks,
            session_manager=SessionManager(temp_db),
            session_task_manager=SessionTaskManager(temp_db),
        )
        response = await handler.evaluate_async(
            HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id="external-projection-retry",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                project_id=task.project_id,
                cwd=str(tmp_path),
                metadata={"_platform_session_id": owner},
                data={},
            )
        )
        assert response.decision == "allow"
    state = variables.get_variables(owner)
    assert injected and guard_attempts == 2
    assert state["claimed_tasks"] == {task_id: f"#{task.seq_num}", other.id: f"#{other.seq_num}"}
    assert state["task_claimed"] is True
    assert state["active_task_id"] == other.id
    assert state["claimed_task_extra_skills"] == ["python"]
    assert state["unrelated_marker"] == "fresh"
    assert tasks.get_task(task_id).claimed_by_session_id == owner
    assert tasks.get_task(other.id).claimed_by_session_id == owner
