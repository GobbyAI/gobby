"""Gobby behavior against sanitized live Droid 0.219.0 hook and session captures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.droid import DroidAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot

pytestmark = pytest.mark.unit

CAPTURES = Path(__file__).parents[1] / "fixtures" / "provider_contracts" / "droid"
HOOK_PAYLOADS = CAPTURES / "hook-payloads-0.219.0.jsonl"
SESSION = CAPTURES / "session-0.219.0.json"


def _hook_captures() -> dict[str, dict[str, Any]]:
    records = [json.loads(line) for line in HOOK_PAYLOADS.read_text(encoding="utf-8").splitlines()]
    assert {record["cli_version"] for record in records} == {"0.219.0"}
    return {record["event"]: record for record in records}


def _session_capture() -> dict[str, Any]:
    capture = json.loads(SESSION.read_text(encoding="utf-8"))
    assert isinstance(capture, dict)
    assert capture["cli_version"] == "0.219.0"
    assert capture["capture_status"] == "live_proven"
    return capture


def _delivered(capture: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    """The daemon's adapter input for a ghook delivery of this hook stdin."""
    payload = capture["payload"]
    return {
        "hook_type": payload["hook_event_name"],
        "source": "droid",
        "input_data": payload,
        **envelope,
    }


def _handle(
    capture: dict[str, Any], response: HookResponse, **envelope: Any
) -> tuple[HookEvent, dict[str, Any]]:
    hook_manager = MagicMock()
    hook_manager.handle.return_value = response
    output = DroidAdapter().handle_native(_delivered(capture, **envelope), hook_manager)
    return hook_manager.handle.call_args.args[0], output


def test_captured_hooks_map_to_unified_events() -> None:
    captures = _hook_captures()
    events = {
        name: _handle(capture, HookResponse(decision="allow"))[0]
        for name, capture in captures.items()
    }

    assert {name: event.event_type for name, event in events.items()} == {
        "session_start": HookEventType.SESSION_START,
        "pre_tool_use_execute": HookEventType.BEFORE_TOOL,
        "post_tool_use_tool_search": HookEventType.AFTER_TOOL,
        "post_tool_use_mcp_call": HookEventType.AFTER_TOOL,
        "stop": HookEventType.STOP,
        "session_end": HookEventType.SESSION_END,
        "pre_tool_use_skill": HookEventType.BEFORE_TOOL,
    }
    for name, event in events.items():
        assert event.source is SessionSource.DROID
        assert event.session_id == captures[name]["payload"]["session_id"]
        assert event.cwd == "<WORKSPACE>"
    assert events["pre_tool_use_execute"].data["tool_name"] == "Bash"
    assert events["pre_tool_use_skill"].data["tool_name"] == "Skill"
    mcp_call = events["post_tool_use_mcp_call"]
    assert (mcp_call.data["mcp_server"], mcp_call.data["mcp_tool"]) == ("gobby", "get_tool_schema")
    assert mcp_call.metadata["is_failure"] is False
    assert events["post_tool_use_tool_search"].metadata["is_failure"] is False
    assert events["stop"].turn_disposition == "completed"


def test_captured_session_end_keeps_its_enqueue_time_when_drained() -> None:
    # Droid kills SessionEnd hook children during teardown, so the installed hook only
    # enqueues; the drained delivery must still describe the moment Droid ended.
    capture = _hook_captures()["session_end"]

    event, output = _handle(
        capture, HookResponse(decision="allow"), _enqueued_at=capture["captured_at"]
    )

    assert event.event_type is HookEventType.SESSION_END
    assert event.timestamp == datetime(2026, 9, 15, 23, 3, 4, tzinfo=UTC)
    assert event.data["reason"] == "other"
    assert output == {"continue": True}


def test_captured_skill_denial_reaches_the_model_through_the_decision_reason() -> None:
    session = _session_capture()
    hook_record = next(
        record["message"]
        for record in session["records"]
        if record.get("type") == "message" and record["message"].get("hookMatcher") == "Skill"
    )
    recorded_output = json.loads(hook_record["hookResults"][0]["stdout"])
    seen_by_model = next(
        block
        for record in session["records"]
        if record.get("type") == "message"
        for block in record["message"]["content"]
        if block.get("tool_use_id") == "tool-call-skill"
    )
    reason, _, menu = recorded_output["hookSpecificOutput"]["permissionDecisionReason"].partition(
        "\n\n"
    )

    _, output = _handle(
        _hook_captures()["pre_tool_use_skill"],
        HookResponse(decision="block", reason=reason, context=menu),
    )

    assert menu.startswith("- `/gobby tasks`")
    assert output == recorded_output
    assert seen_by_model["is_error"] is True
    assert seen_by_model["content"] == (
        f"Error: {output['hookSpecificOutput']['permissionDecisionReason']}"
    )


async def test_captured_sidecar_occupancy_is_the_last_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session_capture()
    transcript = tmp_path / f"{session['records'][0]['id']}.jsonl"
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in session["records"]), encoding="utf-8"
    )
    transcript.with_suffix(".settings.json").write_text(
        json.dumps(session["settings"]), encoding="utf-8"
    )

    parsed = DroidTranscriptParser(transcript_path=transcript).parse_lines(
        transcript.read_text(encoding="utf-8").splitlines(keepends=True), start_index=0
    )
    messages = [record for record in parsed if isinstance(record, ParsedMessage)]
    last_assistant = [message for message in messages if message.role == "assistant"][-1]

    cumulative = session["settings"]["tokenUsage"]
    assert last_assistant.usage is not None
    assert last_assistant.usage.input_tokens == cumulative["inputTokens"]
    assert last_assistant.usage.cache_read_tokens == cumulative["cacheReadTokens"]
    # The live daemon recorded 21213 for this session: the last call's prompt, which
    # carries no cacheCreationTokens key, not the cumulative 115898.
    assert last_assistant.context_used_tokens == 21213

    store = MagicMock()
    store.get_session_totals.return_value = dict.fromkeys(
        ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"), 0
    )
    store.record.return_value = True
    monkeypatch.setattr("gobby.sessions.processor.TokenEventStore", lambda _db: store)
    snapshots: list[ContextUsageSnapshot] = []
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        project_id="project",
        source="droid",
        context_window=None,
        model=session["settings"]["model"],
        context_usage_confidence=None,
    )
    session_manager.update_context_usage.side_effect = lambda _id, snapshot: snapshots.append(
        snapshot
    )
    processor = SessionMessageProcessor(MagicMock(), session_manager=session_manager)

    await processor._persist_usage_events("session", messages)

    assert [snapshot.context_used_tokens for snapshot in snapshots] == [21213]
