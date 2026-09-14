"""Exercise durable holds through the completed-turn idle recovery path."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests.agents.test_lifecycle_monitor import _pane_text, _runtime_of
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)


@pytest.mark.parametrize(
    "ending", ["release", "cancel", "expiry", "orphan", "owner_ended", "lookup_failure"]
)
@pytest.mark.parametrize("pane", ["❯\n", "❯ uv run pytest tests/foo.py\n"])
async def test_coordination_hold_resets_exhausted_idle_recovery_then_resumes(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    ending: str,
    pane: str,
) -> None:
    agent_run_manager = LocalAgentRunManager(temp_db)
    transcript = tmp_path / "held-completed-turn.jsonl"
    _write_codex_lifecycle_transcript(transcript, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2220",
        transcript_path=transcript,
        max_reprompt_attempts=1,
    )
    assert run.child_session_id is not None
    handler = monitor._idle_check_handler
    manager = CoordinationWaitManager(temp_db)

    with _pane_text(monitor, pane):
        assert await monitor.check_idle_agents() == 1
        assert handler._recovery._completed_turn_recovery[run.id].successful_reprompts == 1
        writes_before_hold = list(_runtime_of(monitor).write_log)
        row = manager.register(run.child_session_id, run.parent_session_id, coordination_key="hold")
        _write_codex_lifecycle_transcript(transcript, age_seconds=121)
        for _ in range(2):
            assert await monitor.check_idle_agents() == 0
        assert _runtime_of(monitor).write_log == writes_before_hold
        assert run.id not in handler._recovery._completed_turn_recovery
        assert monitor._idle_detector.get_state(run.id).reprompt_count == 0
        held_run = agent_run_manager.get(run.id)
        assert held_run is not None and held_run.status == "running"

        if ending == "release":
            InterSessionMessageManager(temp_db).create_message(
                from_session=run.parent_session_id,
                to_session=run.child_session_id,
                content="Released",
                message_type="coordination_release",
                metadata_json='{"coordination_key": "hold"}',
            )
        elif ending == "cancel":
            manager.cancel(row["id"], run.child_session_id)
        elif ending == "expiry":
            temp_db.execute(
                "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = %s",
                (row["id"],),
            )
        elif ending == "orphan":
            temp_db.execute(
                "UPDATE coordination_waits SET owner_session_id = %s WHERE id = %s",
                ("dddddddd-dddd-4ddd-8ddd-dddddddd9999", row["id"]),
            )
        elif ending == "owner_ended":
            temp_db.execute(
                "UPDATE sessions SET status = 'completed' WHERE id = %s",
                (run.parent_session_id,),
            )

        # A lookup error preserves the existing watchdog policy: log and continue.
        with patch.object(
            CoordinationWaitManager,
            "has_active_wait",
            side_effect=RuntimeError("offline") if ending == "lookup_failure" else None,
            wraps=manager.has_active_wait,
        ):
            assert await monitor.check_idle_agents() == 1
            assert handler._recovery._completed_turn_recovery[run.id].successful_reprompts == 1
            _write_codex_lifecycle_transcript(transcript, age_seconds=122)
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

    finished = agent_run_manager.get(run.id)
    # The resumed budget exhausts again; with no step workflow or task, the
    # watchdog completes the run instead of failing it.
    assert finished is not None and finished.status == "success"
