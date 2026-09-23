"""Post-health responsiveness with restart recovery work queued."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.sessions import SessionManager
from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, MCPTestClient

pytestmark = pytest.mark.e2e

PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture
def e2e_pre_daemon_setup(e2e_config: tuple[Path, int, int], postgres_db: Any) -> None:
    """Enable code-index maintenance in this test's isolated runtime config."""
    _ = e2e_config
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(values={"code_index.enabled": True}),
        source="restart-responsiveness-test",
    )


def _transcript_line(external_id: str, index: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": external_id,
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "message": {
                "id": f"msg-{index}",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": f"Recovered message {index}"}],
                "usage": {"input_tokens": 100 + index, "output_tokens": 10},
            },
        }
    )


def test_seeded_restart_keeps_wake_health_and_hooks_responsive(
    daemon_instance: DaemonInstance,
    cli_events: CLIEventSimulator,
    mcp_client: MCPTestClient,
    postgres_db: Any,
) -> None:
    """A restart with live sessions, replay hooks, and index work serves requests promptly."""
    external_ids = [f"restart-{uuid.uuid4().hex}" for _ in range(4)]
    session_ids = [
        cli_events.register_session(
            external_id=external_id,
            machine_id=MACHINE_ID,
            source="Claude Code",
            project_id=PROJECT_ID,
            cwd=str(daemon_instance.project_dir),
        )["id"]
        for external_id in external_ids
    ]

    daemon_instance.stop()
    sessions = SessionManager(postgres_db)
    for session_id, external_id in zip(session_ids, external_ids, strict=True):
        transcript = daemon_instance.project_dir / f"{external_id}.jsonl"
        transcript.write_text(
            "\n".join(_transcript_line(external_id, index) for index in range(40)) + "\n",
            encoding="utf-8",
        )
        assert sessions.update(session_id, transcript_path=str(transcript)) is not None

    inbox = daemon_instance.gobby_home / "hooks" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for index, external_id in enumerate(external_ids):
        envelope = {
            "schema_version": 1,
            "enqueued_at": datetime.now(UTC).isoformat(),
            "critical": False,
            "hook_type": "tool-use",
            "source": "claude",
            "input_data": {
                "session_id": external_id,
                "cwd": str(daemon_instance.project_dir),
                "project_id": PROJECT_ID,
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
            },
        }
        (inbox / f"restart-{index}.json").write_text(json.dumps(envelope), encoding="utf-8")

    (daemon_instance.project_dir / "pending_index.py").write_text(
        "def pending_restart_index() -> int:\n    return 1\n", encoding="utf-8"
    )

    daemon_instance.restart()
    health_pass = time.monotonic()
    mcp_client.session_id = session_ids[0]

    started = time.monotonic()
    message = mcp_client.call_tool(
        server_name="gobby-agents",
        tool_name="send_message",
        arguments={
            "from_session": session_ids[0],
            "target": "session",
            "target_id": session_ids[1],
            "content": "restart responsiveness probe",
            "wake": True,
        },
    )
    wake_latency = time.monotonic() - started
    assert message["success"] is True, message
    assert wake_latency < 5.0

    started = time.monotonic()
    health = mcp_client.client.get("/api/auth/status", timeout=2.0)
    health_latency = time.monotonic() - started
    assert health.status_code == 200
    assert health_latency < 2.0

    started = time.monotonic()
    hook = cli_events.tool_use(external_ids[0], "Read", {"file_path": "README.md"})
    hook_latency = time.monotonic() - started
    assert "continue" in hook
    assert hook_latency < 10.0
    assert time.monotonic() - health_pass < 3 * 60
