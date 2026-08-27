"""Isolated-daemon contracts for deferred Grok session context."""

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.session import ChildSessionConfig, ChildSessionManager
from gobby.cli.installers.shared import sync_bundled_content_to_db
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from tests.e2e.conftest import CLIEventSimulator, DaemonInstance

pytestmark = pytest.mark.e2e

PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    """Install production rules and reserved variables in isolated storage."""
    result = sync_bundled_content_to_db(postgres_db)
    assert result["errors"] == []


def _envelope_id(label: str) -> str:
    return f"n-{int(time.time() * 1000)}-{label}-{uuid.uuid4().hex[:8]}"


def _component_text(components: object) -> str:
    assert isinstance(components, list)
    texts = [item["text"] for item in components if isinstance(item, dict)]
    assert all(isinstance(text, str) for text in texts)
    return "\n".join(texts)


def _session_count(
    postgres_db: HubDatabase,
    *,
    external_id: str,
    source: str,
) -> int:
    row = postgres_db.execute(
        """
        SELECT COUNT(*) AS count
        FROM sessions
        WHERE external_id = %s AND project_id = %s AND source = %s
        """,
        (external_id, PROJECT_ID, source),
    ).fetchone()
    assert row is not None
    return int(row["count"])


def _write_retained_envelope(
    path: Path,
    *,
    external_id: str,
    project_dir: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "enqueued_at": datetime.now(UTC).isoformat(),
        "critical": False,
        "hook_type": "pre_tool_use",
        "source": "grok",
        "input_data": {
            "session_id": external_id,
            "cwd": project_dir,
            "project_id": PROJECT_ID,
            "tool_name": "read_file",
            "tool_input": {},
        },
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_spawned_grok_shell_briefing_and_p2p_acknowledgment(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
) -> None:
    sessions = SessionManager(postgres_db)
    machine_id = get_machine_id()
    assert machine_id is not None
    parent = sessions.register(
        external_id=f"grok-parent-{uuid.uuid4()}",
        machine_id=machine_id,
        source="codex",
        project_id=PROJECT_ID,
    )
    child_sessions = ChildSessionManager(sessions, max_agent_depth=5)
    spawned_grok = child_sessions.create_child_session(
        ChildSessionConfig(
            parent_session_id=parent.id,
            project_id=PROJECT_ID,
            machine_id=machine_id,
            source="grok",
            sandbox_enabled=True,
        )
    )
    run_manager = LocalAgentRunManager(postgres_db)
    spawned_run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=spawned_grok.id,
        claimed_session_id=spawned_grok.id,
        provider="grok",
        prompt="Exercise spawned Grok pending-context delivery",
        run_id=str(uuid.uuid4()),
    )
    child_sessions.update_terminal_pickup_metadata(
        spawned_grok.id,
        agent_run_id=spawned_run.id,
    )
    external_id = f"grok-spawned-{uuid.uuid4()}"
    project_dir = str(daemon_instance.project_dir)
    terminal_context = {
        "gobby_session_id": spawned_grok.id,
        "gobby_agent_run_id": spawned_run.id,
        "gobby_parent_session_id": parent.id,
        "gobby_project_id": PROJECT_ID,
    }

    started = cli_events.session_start(
        external_id,
        machine_id=machine_id,
        cli_source="grok",
        project_id=PROJECT_ID,
        cwd=project_dir,
        terminal_context=terminal_context,
    )

    assert started["continue"] is True
    assert started["decision"] == "allow"
    bound = sessions.find_by_external_id(external_id, PROJECT_ID, "grok")
    assert bound is not None
    assert bound.id == spawned_grok.id
    refreshed_run = run_manager.get(spawned_run.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "running"
    cli_events.user_prompt_submit(
        external_id,
        prompt="run the probe",
        source="grok",
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    variable_manager = SessionVariableManager(postgres_db)
    briefing = _component_text(
        variable_manager.get_variables(spawned_grok.id)["grok_pending_briefing"]
    )
    assert "Gobby Session ID:" in briefing
    message_manager = InterSessionMessageManager(postgres_db)
    pending_message = message_manager.create_message(
        parent.id,
        spawned_grok.id,
        "SPAWNED-GROK-P2P",
    )
    command = {"command": "git status --short | head -3"}
    first_envelope = _envelope_id("spawned-first-shell")

    first = cli_events.grok_pre_tool_use(
        external_id,
        "run_terminal_command",
        first_envelope,
        command,
        project_id=PROJECT_ID,
        cwd=project_dir,
    )

    assert first["decision"] == "deny"
    assert "Gobby Session ID:" in first["reason"]
    assert pending_message.content in first["reason"]
    before_ack = variable_manager.get_variables(spawned_grok.id)
    assert before_ack["grok_pending_delivery"]["envelope_id"] == first_envelope
    stored_message = message_manager.get_message(pending_message.id)
    assert stored_message is not None
    assert stored_message.delivered_at is None

    retry = cli_events.grok_pre_tool_use(
        external_id,
        "run_terminal_command",
        _envelope_id("spawned-shell-retry"),
        command,
        project_id=PROJECT_ID,
        cwd=project_dir,
    )

    assert retry == {"continue": True}
    after_ack = variable_manager.get_variables(spawned_grok.id)
    assert after_ack["grok_pending_briefing"] == []
    assert "grok_pending_delivery" not in after_ack
    delivered_message = message_manager.get_message(pending_message.id)
    assert delivered_message is not None
    assert delivered_message.delivered_at is not None


def test_grok_session_deferral_contract(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
) -> None:
    sessions = SessionManager(postgres_db)
    project_dir = str(daemon_instance.project_dir)
    claude_external_id = f"claude-deferral-{uuid.uuid4()}"
    grok_external_id = f"grok-deferral-{uuid.uuid4()}"
    for external_id, source in (
        (grok_external_id, "grok"),
        (claude_external_id, "claude"),
    ):
        response = cli_events.session_start(
            external_id,
            cli_source=source,
            project_id=PROJECT_ID,
            cwd=project_dir,
        )
        assert response["continue"] is True
        if source == "grok":
            assert response["decision"] == "allow"
        assert sessions.find_by_external_id(external_id, PROJECT_ID, source) is None

    claude_prompt = cli_events.user_prompt_submit(
        claude_external_id,
        prompt="hello",
        source="claude",
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    claude_session = sessions.find_by_external_id(claude_external_id, PROJECT_ID, "claude")
    assert claude_session is not None
    assert claude_prompt["hookSpecificOutput"]["additionalContext"]

    grok_prompt = cli_events.user_prompt_submit(
        grok_external_id,
        prompt="hello",
        source="grok",
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    grok_session = sessions.find_by_external_id(grok_external_id, PROJECT_ID, "grok")
    assert grok_session is not None
    assert "additionalContext" not in grok_prompt.get("hookSpecificOutput", {})
    variable_manager = SessionVariableManager(postgres_db)
    variables = variable_manager.get_variables(grok_session.id)
    startup_briefing = _component_text(variables["grok_pending_briefing"])
    assert startup_briefing

    message_manager = InterSessionMessageManager(postgres_db)
    pending_message = message_manager.create_message(
        grok_session.id,
        grok_session.id,
        "P2P message held until Grok acknowledges its briefing",
    )
    first_envelope = _envelope_id("first-tool")
    first_tool = cli_events.grok_pre_tool_use(
        grok_external_id,
        "read_file",
        first_envelope,
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert first_tool["decision"] == "deny"
    assert first_tool["continue"] is True
    assert startup_briefing in first_tool["reason"]
    assert pending_message.content in first_tool["reason"]
    before_ack = variable_manager.get_variables(grok_session.id)
    assert before_ack["grok_pending_delivery"]["envelope_id"] == first_envelope
    stored_message = message_manager.get_message(pending_message.id)
    assert stored_message is not None
    assert stored_message.delivered_at is None

    acknowledged_tool = cli_events.grok_pre_tool_use(
        grok_external_id,
        "read_file",
        _envelope_id("acknowledged-tool"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert acknowledged_tool == {"continue": True}
    after_ack = variable_manager.get_variables(grok_session.id)
    assert "grok_pending_delivery" not in after_ack
    delivered_message = message_manager.get_message(pending_message.id)
    assert delivered_message is not None
    assert delivered_message.delivered_at is not None

    retry_text = "Retry this briefing after simulated stdout failure."
    variable_manager.merge_variables(
        grok_session.id,
        {"grok_pending_briefing": [{"id": "test:retry", "text": retry_text}]},
    )
    failed_envelope = _envelope_id("stdout-failure")
    failed_delivery = cli_events.grok_pre_tool_use(
        grok_external_id,
        "read_file",
        failed_envelope,
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert failed_delivery["decision"] == "deny"
    assert retry_text in failed_delivery["reason"]
    retained_path = daemon_instance.gobby_home / "hooks" / "inbox" / f"{failed_envelope}.json"
    _write_retained_envelope(
        retained_path,
        external_id=grok_external_id,
        project_dir=project_dir,
    )

    requeue_hook = cli_events.post_tool_use(
        grok_external_id,
        cli_source="grok",
        input_data={"tool_name": "read_file", "tool_result": "ok", "success": True},
        project_id=PROJECT_ID,
    )
    assert requeue_hook["decision"] == "allow"
    assert not retained_path.exists()
    retry_delivery = cli_events.grok_pre_tool_use(
        grok_external_id,
        "read_file",
        _envelope_id("retry-tool"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert retry_delivery["decision"] == "deny"
    assert retry_delivery["reason"] == failed_delivery["reason"]
    cli_events.post_tool_use(
        grok_external_id,
        cli_source="grok",
        input_data={"tool_name": "read_file", "tool_result": "ok", "success": True},
        project_id=PROJECT_ID,
    )

    variable_manager.merge_variables(grok_session.id, {"tool_block_pending": True})
    gated_stop = cli_events.grok_stop(
        grok_external_id,
        _envelope_id("real-gate"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert gated_stop["decision"] == "block"
    assert gated_stop["continue"] is True

    text_only_briefing = "Deliver this briefing through a tool-free Stop."
    variable_manager.merge_variables(
        grok_session.id,
        {"grok_pending_briefing": [{"id": "test:text-only", "text": text_only_briefing}]},
    )
    briefing_stop = cli_events.grok_stop(
        grok_external_id,
        _envelope_id("briefing-stop"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert briefing_stop["decision"] == "block"
    assert briefing_stop["continue"] is True
    assert text_only_briefing in briefing_stop["hookSpecificOutput"]["additionalContext"]
    following_stop = cli_events.grok_stop(
        grok_external_id,
        _envelope_id("following-stop"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert following_stop["decision"] == "allow"

    variable_manager.merge_variables(
        grok_session.id,
        {
            "grok_pending_turn_context": [
                {"id": "test:turn-only", "text": "Turn-only context must not loop."}
            ]
        },
    )
    turn_only_stop = cli_events.grok_stop(
        grok_external_id,
        _envelope_id("turn-only-stop"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert turn_only_stop["decision"] == "allow"

    original_internal_id = grok_session.id
    assert _session_count(postgres_db, external_id=grok_external_id, source="grok") == 1
    compact_start = cli_events.session_start(
        grok_external_id,
        cli_source="grok",
        session_start_source="compact",
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert compact_start["decision"] == "allow"
    rebound_session = sessions.find_by_external_id(grok_external_id, PROJECT_ID, "grok")
    assert rebound_session is not None
    assert rebound_session.id == original_internal_id
    assert _session_count(postgres_db, external_id=grok_external_id, source="grok") == 1

    variable_manager.merge_variables(
        grok_session.id,
        {"compact_handoff_inject_pending": True},
    )
    compact_prompt = cli_events.user_prompt_submit(
        grok_external_id,
        prompt="continue",
        source="grok",
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert "additionalContext" not in compact_prompt.get("hookSpecificOutput", {})
    compact_variables = variable_manager.get_variables(grok_session.id)
    compact_briefing = _component_text(compact_variables["grok_pending_briefing"])
    assert "Continuation Context" in compact_briefing
    assert compact_variables["compact_handoff_inject_pending"] is False
    compact_delivery = cli_events.grok_pre_tool_use(
        grok_external_id,
        "read_file",
        _envelope_id("compact-delivery"),
        project_id=PROJECT_ID,
        cwd=project_dir,
    )
    assert compact_delivery["decision"] == "deny"
    assert "Continuation Context" in compact_delivery["reason"]
