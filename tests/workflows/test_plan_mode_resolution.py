"""Authoritative plan-mode resolution across session surfaces."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observer_plan_mode import resolve_plan_mode

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


class _SessionManager:
    def __init__(self, session: Any | None) -> None:
        self.session = session

    def get(self, _session_id: str) -> Any | None:
        return self.session


def _event(
    source: SessionSource,
    *,
    data: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
    ],
)
def test_managed_web_chat_runtime_mode_is_authoritative_without_prompt_tags(
    source: SessionSource,
) -> None:
    variables = {"chat_mode": "normal", "mode_level": 1, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="normal")
    event = _event(
        source,
        data={"prompt": "inspect the repository"},
        metadata={"session_type": "web_chat", "chat_mode": "plan"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))

    assert variables["chat_mode"] == "plan"
    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def test_mode_transition_emits_one_debug_record_and_unchanged_mode_emits_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    variables = {"chat_mode": "normal", "mode_level": 1, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="normal")
    event = _event(
        SessionSource.CLAUDE,
        data={"prompt": "inspect the repository"},
        metadata={"session_type": "web_chat", "chat_mode": "plan"},
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.workflows.observers"):
        resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))
        resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith(f"Session {SESSION_ID}: effective mode changed")
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.DEBUG
    assert record.__dict__["mode"] == "plan"
    assert record.__dict__["mode_level"] == 0
    assert record.__dict__["plan_mode"] is True
    assert record.__dict__["resolution_reason"] == "managed web-chat runtime metadata"


def test_managed_web_chat_uses_persisted_mode_when_runtime_mode_is_missing() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="plan")
    event = _event(SessionSource.CLAUDE, metadata={"session_type": "web_chat"})

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(persisted))

    assert variables["chat_mode"] == "plan"
    assert variables["plan_mode"] is True


def test_runtime_plan_approval_switches_to_execution_synchronously() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    persisted = SimpleNamespace(session_type="web_chat", chat_mode="plan")
    manager = _SessionManager(persisted)

    resolve_plan_mode(
        _event(
            SessionSource.CODEX,
            metadata={"session_type": "web_chat", "chat_mode": "plan"},
        ),
        variables,
        SESSION_ID,
        manager,
    )
    resolve_plan_mode(
        _event(
            SessionSource.CODEX,
            metadata={"session_type": "web_chat", "chat_mode": "normal"},
        ),
        variables,
        SESSION_ID,
        manager,
    )

    assert variables["chat_mode"] == "normal"
    assert variables["mode_level"] == 1
    assert variables["plan_mode"] is False


def test_turn_start_resolves_mode_before_context_pressure_accounting() -> None:
    variables = {
        "chat_mode": "normal",
        "mode_level": 1,
        "plan_mode": False,
        "parent_turn_seq": 7,
        "turns_since_compact": 3,
    }
    persisted = SimpleNamespace(
        session_type="web_chat",
        chat_mode="normal",
        context_usage_ratio=0.90,
        context_used_tokens=None,
        context_window=1_000_000,
    )
    event = _event(
        SessionSource.QWEN,
        data={"prompt": "inspect the repository"},
        metadata={"session_type": "web_chat", "chat_mode": "plan"},
    )
    handler = WorkflowHookHandler(session_manager=_SessionManager(persisted))

    failures = handler._run_observers(event, SESSION_ID, variables)

    assert failures == set()
    assert variables["plan_mode"] is True
    assert variables["context_compact_guidance_message"] == ""
    assert variables["turns_since_compact"] == 3


def test_structured_hook_mode_precedes_provider_state_and_prompt_markers() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.CLAUDE,
        data={
            "chat_mode": "normal",
            "permission_mode": "plan",
            "prompt": '<plan-mode status="active">',
        },
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["chat_mode"] == "normal"
    assert variables["plan_mode"] is False


def test_provider_native_mode_precedes_prompt_markers() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.CLAUDE,
        data={"permission_mode": "plan", "prompt": "Exited Plan Mode"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def test_proposed_plan_alone_does_not_activate_plan_mode() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.DROID,
        data={"prompt": "<proposed_plan>1. Inspect code</proposed_plan>"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["mode_level"] == 2
    assert variables["plan_mode"] is False


def test_bare_plan_mode_tag_is_fallback_evidence() -> None:
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.DROID,
        data={"prompt": "<plan-mode>Research only</plan-mode>"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(None))

    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True


def test_codex_uses_latest_turn_context_mode(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    records = [
        {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "plan"}}},
        {"type": "event_msg", "payload": {"type": "user_message"}},
        {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "default"}}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = SimpleNamespace(session_type="terminal", transcript_path=str(transcript))
    variables = {"chat_mode": "bypass", "mode_level": 0, "plan_mode": True}
    event = _event(
        SessionSource.CODEX,
        data={"prompt": '<plan-mode status="active">'},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(session))

    assert variables["chat_mode"] == "normal"
    assert variables["mode_level"] == 1
    assert variables["plan_mode"] is False


@pytest.mark.parametrize("transcript_state", ["missing", "malformed"])
def test_codex_transcript_failure_falls_back_to_prompt_marker(
    tmp_path: Path, transcript_state: str
) -> None:
    transcript = tmp_path / "codex.jsonl"
    if transcript_state == "malformed":
        transcript.write_text("{not-json}\n")
    session = SimpleNamespace(session_type="terminal", transcript_path=str(transcript))
    variables = {"chat_mode": "bypass", "mode_level": 2, "plan_mode": False}
    event = _event(
        SessionSource.CODEX,
        data={"prompt": "<plan-mode>Research only</plan-mode>"},
        metadata={"session_type": "terminal"},
    )

    resolve_plan_mode(event, variables, SESSION_ID, _SessionManager(session))

    assert variables["mode_level"] == 0
    assert variables["plan_mode"] is True
