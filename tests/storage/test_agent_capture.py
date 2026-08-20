from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from tests.agents.terminal_fixtures import make_live_terminal

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _create_run(
    db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
    *,
    suffix: str,
    child_status: str = "active",
) -> tuple[LocalAgentRunManager, str, str]:
    parent = session_manager.register(
        external_id=f"capture-parent-{suffix}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id=f"capture-child-{suffix}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    if child_status != "active":
        session_manager.update_status(child.id, child_status)
    manager = LocalAgentRunManager(db)
    run = manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="capture test",
    )
    manager.start(run.id)
    manager.update_runtime(run.id)
    _live_run = manager.get(run.id)
    assert _live_run is not None
    make_live_terminal(_live_run, db=manager.db, session_name=f"gobby-{suffix}")


    return manager, run.id, child.id


def test_capture_slot_initializes_replaces_and_rejects_stale_writes(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    manager, run_id, _child_id = _create_run(
        temp_db,
        session_manager,
        sample_project,
        suffix="slot",
    )
    intent = manager.record_termination_intent(
        run_id,
        action="fail",
        reason="watchdog",
        result_prefix="existing prefix",
    )
    assert intent is not None
    assert intent.capture_revision == 0

    marker = "--- GOBBY TMUX CAPTURE capture-a ---"
    assert (
        manager.replace_capture_slot(
            run_id,
            capture_id="capture-a",
            expected_revision=1,
            marker=marker,
            slot_content="stale initialization",
        )
        is None
    )
    first = manager.replace_capture_slot(
        run_id,
        capture_id="capture-a",
        expected_revision=0,
        marker=marker,
        slot_content=f"{marker}\nfirst output\n--- END GOBBY TMUX CAPTURE capture-a ---",
    )
    assert first is not None
    assert first.capture_revision == 1
    assert first.result == (
        "existing prefix\n\n--- GOBBY TMUX CAPTURE capture-a ---\n"
        "first output\n--- END GOBBY TMUX CAPTURE capture-a ---"
    )

    replaced = manager.replace_capture_slot(
        run_id,
        capture_id="capture-a",
        expected_revision=1,
        marker=marker,
        slot_content=f"{marker}\nsecond output\n--- END GOBBY TMUX CAPTURE capture-a ---",
    )
    assert replaced is not None
    assert replaced.capture_revision == 2
    assert replaced.result is not None
    assert "first output" not in replaced.result
    assert replaced.result.count(marker) == 1
    assert replaced.result.startswith("existing prefix\n\n")

    assert (
        manager.replace_capture_slot(
            run_id,
            capture_id="foreign",
            expected_revision=2,
            marker="--- GOBBY TMUX CAPTURE foreign ---",
            slot_content="foreign",
        )
        is None
    )

    manager.fail(run_id, error="done")
    assert (
        manager.replace_capture_slot(
            run_id,
            capture_id="capture-a",
            expected_revision=2,
            marker=marker,
            slot_content="terminal overwrite",
        )
        is None
    )


def test_termination_candidates_include_intent_and_terminal_child_only(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    manager, intended_id, _ = _create_run(
        temp_db,
        session_manager,
        sample_project,
        suffix="intent",
    )
    manager.record_termination_intent(intended_id, action="timeout", reason="deadline")

    _manager, terminal_child_id, _ = _create_run(
        temp_db,
        session_manager,
        sample_project,
        suffix="terminal-child",
        child_status="expired",
    )
    _manager, active_child_id, _ = _create_run(
        temp_db,
        session_manager,
        sample_project,
        suffix="active-child",
    )

    no_tmux_manager, no_tmux_id, no_tmux_child = _create_run(
        temp_db,
        session_manager,
        sample_project,
        suffix="no-tmux",
        child_status="expired",
    )
    no_tmux_manager.clear_terminal_id(no_tmux_id, "gobby-no-tmux")

    candidate_ids = {
        run.id for run in manager.list_termination_candidates(machine_id=require_machine_id())
    }
    assert intended_id in candidate_ids
    assert terminal_child_id in candidate_ids
    assert active_child_id not in candidate_ids
    assert no_tmux_id not in candidate_ids


def test_terminal_transition_clears_intent_and_preserves_capture_result(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    manager, run_id, _ = _create_run(
        temp_db,
        session_manager,
        sample_project,
        suffix="terminal",
    )
    intent = manager.record_termination_intent(run_id, action="fail", reason="failure")
    assert intent is not None
    marker = "--- GOBBY TMUX CAPTURE stable ---"
    captured = manager.replace_capture_slot(
        run_id,
        capture_id="stable",
        expected_revision=0,
        marker=marker,
        slot_content=f"{marker}\nfull output\n--- END GOBBY TMUX CAPTURE stable ---",
    )
    assert captured is not None

    failed = manager.fail(run_id, error="failure")
    assert failed is not None
    assert failed.status == "error"
    assert failed.result == captured.result
    assert failed.pending_terminal_action is None
    assert failed.pending_terminal_reason is None
    assert failed.termination_requested_at is None
    assert failed.terminal_id is None
