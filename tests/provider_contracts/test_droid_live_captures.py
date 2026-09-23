"""Gobby behavior against sanitized live Droid provider captures."""

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
LIFECYCLE = CAPTURES / "turn-lifecycle-0.223.0.json"


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


def _lifecycle_capture() -> dict[str, Any]:
    capture = json.loads(LIFECYCLE.read_text(encoding="utf-8"))
    assert isinstance(capture, dict)
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


def test_lifecycle_capture_has_managed_distinct_provenance() -> None:
    capture = _lifecycle_capture()
    cases = capture["cases"]

    assert capture["version"] == "0.223.0"
    assert capture["capture_date"] == "2026-09-19"
    assert capture["binary_sha256"] == (
        "efed63e905bcc6f7e17d9deaf6542b4a4d5cff7dcb3a7a4315a31c471050f925"
    )
    assert capture["provenance"]["launcher"] == "gobby-agents:spawn_agent"
    assert capture["provenance"]["direct_binary_invocation"] is False
    assert set(cases) == {
        "normal_answer",
        "prose_question",
        "structured_question",
        "permission_wait",
        "approval",
        "denial",
        "user_interruption",
        "non_user_failure",
        "replacement_prompt_race",
        "session_exit",
    }
    for identity_key in ("run_id", "child_session_id", "external_session_id"):
        assert len({case[identity_key] for case in cases.values()}) == 10
    assert "/Users/" not in LIFECYCLE.read_text(encoding="utf-8")


def test_lifecycle_completion_and_structured_wait_slices() -> None:
    cases = _lifecycle_capture()["cases"]

    normal = cases["normal_answer"]
    assert normal["hook_slice"][-1]["event"] == "Stop"
    assert normal["transcript_slice"][-1]["text"] == "GOBBY_TL_NORMAL_OK"

    prose = cases["prose_question"]
    assert prose["hook_slice"][-1]["event"] == "Stop"
    assert prose["transcript_slice"][-1]["text"] == "Should I continue?"

    structured = cases["structured_question"]
    structured_hooks = structured["hook_slice"]
    structured_transcript = structured["transcript_slice"]
    assert structured_hooks[-1]["notification_type"] == "elicitation_dialog"
    assert all(event["event"] != "Stop" for event in structured_hooks)
    assert structured_transcript[1]["hook_tool_call_id"] == structured["tool_id"]
    assert structured_transcript[1]["parent_id"] == structured_transcript[2]["parent_id"]


def test_lifecycle_permission_resolution_slices() -> None:
    cases = _lifecycle_capture()["cases"]

    waiting = cases["permission_wait"]
    waiting_events = waiting["hook_slice"]
    assert waiting_events[1]["permission_decision"] == "ask"
    assert waiting_events[-1]["notification_type"] == "permission_prompt"
    assert not {"PostToolUse", "Stop"} & {event["event"] for event in waiting_events}

    approval = cases["approval"]
    assert approval["hook_slice"][-1]["event"] == "PostToolUse"
    approved_result = approval["transcript_slice"][-1]
    assert approved_result["tool_id"] == approval["tool_id"]
    assert approved_result["is_error"] is False

    denial = cases["denial"]
    denied_result = denial["transcript_slice"][-1]
    assert denied_result == {
        "id": "291439e2-4f3f-4ed8-837b-862006b0def4",
        "at": "2026-09-20T03:48:29.532Z",
        "kind": "tool_result",
        "tool_id": denial["tool_id"],
        "is_error": True,
        "content": "Tool execution cancelled by user",
    }
    assert "no whole-turn cancellation marker in bounded slice" in denial["negative_evidence"]


def test_lifecycle_interruption_and_non_user_failure_remain_distinct() -> None:
    cases = _lifecycle_capture()["cases"]

    interruption = cases["user_interruption"]
    assert [event["event"] for event in interruption["hook_slice"]][-2:] == [
        "Stop",
        "Notification",
    ]
    assert interruption["hook_slice"][-1]["notification_type"] == "idle_prompt"
    records_by_id = {
        record["id"]: record for record in interruption["transcript_slice"] if "id" in record
    }
    lineage = set()
    current_id = interruption["hook_slice"][-1]["message_id"]
    while current_id in records_by_id:
        lineage.add(current_id)
        current_id = records_by_id[current_id].get("parent_id")
    assert interruption["provider_turn_id"] == current_id
    outcome = interruption["transcript_slice"][-1]
    assert outcome["kind"] == "agent_turn_outcome"
    assert outcome["turn_id"] == interruption["provider_turn_id"]
    assert outcome["reason"] == "cancelled"

    failure = cases["non_user_failure"]
    stopped = failure["hook_slice"][1]
    assert stopped["continue"] is False
    assert stopped["stop_reason"] == "GOBBY_TL_NON_USER_FAILURE:n22308i"
    assert failure["transcript_slice"][-1]["reason"] == "completed"
    assert "no whole-turn cancellation marker in bounded slice" in failure["negative_evidence"]


def test_lifecycle_replacement_race_and_session_exit_slices() -> None:
    cases = _lifecycle_capture()["cases"]

    race = cases["replacement_prompt_race"]
    stale_result = race["transcript_slice"][1]
    replacement = race["transcript_slice"][-1]
    assert stale_result["content"] == "Error: GOBBY_TL_STALE_RESULT:n22309i"
    assert replacement["text"] == "GOBBY_TL_REPLACEMENT_OK_n22309i"
    assert replacement["at"] > stale_result["at"]
    assert race["message_action"]["surface"] == "gobby-agents:send_message"

    session_exit = cases["session_exit"]
    end_event = session_exit["hook_slice"][-1]
    assert end_event["event"] == "SessionEnd"
    assert end_event["reason"] == "other"
    assert session_exit["transcript_slice"][-1]["event"] == "SessionEnd"
