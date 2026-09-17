"""Claude API errors that end a turn terminalize the run instead of stagnating."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.autonomous.stuck_detector import StuckDetectionResult, StuckDetector
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.agents.test_lifecycle_monitor import _pane_text, _runtime_of
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    LOCAL_MACHINE_ID,
    _make_idle_monitor_run,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

_AUTH_TEXT = (
    "Please run /login · API Error: 401 OAuth access token has expired. "
    "Re-authenticate to continue."
)
_BAD_GATEWAY_TEXT = "API Error: 502 status code (no body). This is a server-side issue"
_AUTH_ERROR = ("authentication_failed", 401, _AUTH_TEXT)
_BAD_GATEWAY_ERROR = ("server_error", 502, _BAD_GATEWAY_TEXT)


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


def _write_api_error_transcript(
    path: Path,
    *,
    error: str,
    status: int,
    text: str,
    progress_after_error: bool = False,
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    records: list[dict[str, object]] = [
        {"type": "user", "timestamp": timestamp, "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "timestamp": timestamp,
            "error": error,
            "apiErrorStatus": status,
            "isApiErrorMessage": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
    ]
    if progress_after_error:
        records.append(
            {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "private progress"}],
                },
            }
        )
    records.append(
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": timestamp,
            "durationMs": 1200,
            "messageCount": 2,
        }
    )
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


@pytest.mark.parametrize("api_error", [_AUTH_ERROR, _BAD_GATEWAY_ERROR], ids=["401", "502"])
async def test_turn_ending_api_error_fails_recent_claude_run_and_notifies_parent(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    api_error: tuple[str, int, str],
) -> None:
    error, status, text = api_error
    notifications: list[dict[str, Any]] = []

    async def wake_parent(
        _session_id: str,
        _message: str,
        result: dict[str, Any],
    ) -> dict[str, bool]:
        notifications.append(result)
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake_parent)
    transcript_path = tmp_path / "claude-api-error.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=f"eeeeeeee-eeee-4eee-8eee-eeeeeeee0{status}",
        transcript_path=transcript_path,
        child_source="claude",
        session_age_seconds=1,
        completion_registry=registry,
    )
    registry.register(run.id, [run.parent_session_id])
    _write_api_error_transcript(transcript_path, error=error, status=status, text=text)

    with _pane_text(monitor, "❯\n"):
        handled = await monitor.check_idle_agents()

    expected_error = f"Claude provider error: {error} (HTTP {status}): {text}"
    assert handled == 1
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "error"
    assert updated_run.terminal_reason == "provider_error"
    assert updated_run.error == expected_error
    assert [(item["status"], item["error"]) for item in notifications] == [
        ("error", expected_error)
    ]
    assert _runtime_of(monitor).write_log == []


async def test_api_error_followed_by_progress_keeps_recent_claude_run_alive(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "claude-transient-api-error.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0503",
        transcript_path=transcript_path,
        child_source="claude",
        session_age_seconds=1,
    )
    _write_api_error_transcript(
        transcript_path,
        error="server_error",
        status=502,
        text=_BAD_GATEWAY_TEXT,
        progress_after_error=True,
    )

    with _pane_text(monitor, "❯\n"):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "running"
    assert _runtime_of(monitor).write_log == []


@pytest.mark.parametrize(
    ("progress_after_error", "expected_error", "expected_reason"),
    [
        (
            False,
            f"Claude provider error: server_error (HTTP 502): {_BAD_GATEWAY_TEXT}",
            "provider_error",
        ),
        (True, "autonomous stuck: No progress events for 634 seconds", None),
    ],
    ids=["current-provider-error", "error-followed-by-progress"],
)
async def test_stuck_sweep_names_current_provider_error(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    progress_after_error: bool,
    expected_error: str,
    expected_reason: str | None,
) -> None:
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True,
        reason="No progress events for 634 seconds",
        layer="progress_stagnation",
        suggested_action="stop",
    )
    transcript_path = tmp_path / "claude-stuck-api-error.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=f"eeeeeeee-eeee-4eee-8eee-eeeeeeee050{int(progress_after_error)}",
        transcript_path=transcript_path,
        child_source="claude",
        stuck_detector=cast(StuckDetector, stuck_detector),
    )
    _write_api_error_transcript(
        transcript_path,
        error="server_error",
        status=502,
        text=_BAD_GATEWAY_TEXT,
        progress_after_error=progress_after_error,
    )

    with _pane_text(monitor, "❯\n"):
        handled = await monitor.check_autonomous_stuck_agents()

    assert handled == 1
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "error"
    assert updated_run.error == expected_error
    assert updated_run.terminal_reason == expected_reason
