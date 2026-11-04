"""Claude compaction hooks through HookManager and the real rule engine (#25683).

A subagent's auto-compaction shares the parent's session id, so its hooks must never reach
the parent's session_start and pre_compact rules. The parent's own auto-compaction must
still replay them, and the parent's manual compaction must run them immediately. The rules
here are synthetic; no bundled definition is named.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.compaction_ownership import CompactionOwnership
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "18b6d896-29ef-483f-86f3-110271af4374"

SYNTHETIC_RULES: list[tuple[str, dict[str, Any]]] = [
    (
        "reset-on-compact-start",
        {
            "event": "session_start",
            "when": "event.data.get('source') in ['compact']",
            "priority": 10,
            "effects": [{"type": "set_variable", "variable": "unlocked_tools", "value": []}],
        },
    ),
    (
        "mark-pre-compact",
        {
            "event": "pre_compact",
            "priority": 10,
            "effects": [{"type": "set_variable", "variable": "pre_compact_seen", "value": True}],
        },
    ),
]


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _transcript_line(record: dict[str, Any]) -> bytes:
    return json.dumps(record).encode() + b"\n"


def _engine(db: HubDatabase) -> RuleEngine:
    manager = RuleDefinitionManager(db)
    for index, (_label, rule) in enumerate(SYNTHETIC_RULES):
        body = RuleDefinitionBody.model_validate(
            {key: rule[key] for key in ("event", "when", "effects") if key in rule}
        )
        # Installed gobby-tagged rows are the trusted writers of runtime-managed variables.
        manager.create(
            name=f"synthetic-compaction-rule-{index}",
            tags=["gobby", "default"],
            priority=int(rule.get("priority", 100)),
            definition_json=body.model_dump(mode="json"),
        )
    return RuleEngine(db)


def _event(event_type: HookEventType, data: dict[str, Any], transcript: str) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"cwd": "/tmp", "transcript_path": transcript, **data},
    )


class _Session:
    """HookManager wired to the real rule engine; only the handler stage is a stand-in."""

    def __init__(self, db: HubDatabase, transcript: Path) -> None:
        self.transcript = transcript
        self.engine = _engine(db)
        self.variables: dict[str, Any] = {"unlocked_tools": ["mcp__gobby__call_tool"]}
        self.evaluated: list[HookEventType] = []
        self.manager = MagicMock()
        self.manager._compaction_ownership = CompactionOwnership()
        self.manager._handle_after_daemon_ready = self._evaluate
        self.manager._handle_internal = self._replay_sync
        self.manager._handle_internal_async = self._replay_async

    def _evaluate(self, event: HookEvent) -> HookResponse:
        self.evaluated.append(event.event_type)
        return asyncio.run(
            self.engine.evaluate(event, session_id=SESSION_ID, variables=self.variables)
        )

    def _replay_sync(self, event: HookEvent) -> HookResponse:
        return HookManager._handle_internal(self.manager, event)

    async def _replay_async(self, event: HookEvent) -> HookResponse:
        return await HookManager._handle_internal_async(self.manager, event)

    def send_sync(self, event: HookEvent) -> HookResponse:
        return HookManager._handle_internal(self.manager, event)

    async def send_async(self, event: HookEvent) -> HookResponse:
        return await HookManager._handle_internal_async(self.manager, event)

    def event(self, event_type: HookEventType, **data: Any) -> HookEvent:
        return _event(event_type, data, str(self.transcript))

    def parent_boundary(self, uuid: str) -> None:
        record = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": uuid,
            "timestamp": _stamp(datetime.now(UTC)),
            "compactMetadata": {"trigger": "auto"},
        }
        with self.transcript.open("ab") as handle:
            handle.write(_transcript_line(record))


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "parent.jsonl"
    older = {
        "type": "user",
        "uuid": "u-0",
        "timestamp": _stamp(datetime.now(UTC) - timedelta(minutes=3)),
        "message": {"role": "user", "content": "earlier"},
    }
    path.write_bytes(_transcript_line(older))
    return path


@pytest.fixture
def session(temp_db: HubDatabase, transcript: Path) -> Iterator[_Session]:
    with (
        patch("gobby.hooks.hook_manager.ensure_daemon_ready", return_value=None),
        patch(
            "gobby.hooks.hook_manager.ensure_daemon_ready_async",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield _Session(temp_db, transcript)


@pytest.fixture(params=["sync", "async"])
def send(request: pytest.FixtureRequest, session: _Session) -> Callable[[HookEvent], HookResponse]:
    if request.param == "sync":
        return session.send_sync
    return lambda event: asyncio.run(session.send_async(event))


def test_parent_manual_compaction_runs_the_rules_immediately(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    pre = session.event(HookEventType.PRE_COMPACT, trigger="manual")
    start = session.event(HookEventType.SESSION_START, source="compact")

    assert send(pre).decision == "allow"
    assert session.variables.get("pre_compact_seen") is True

    send(start)

    assert session.variables["unlocked_tools"] == []
    assert session.evaluated == [HookEventType.PRE_COMPACT, HookEventType.SESSION_START]


def test_subagent_auto_compaction_never_reaches_the_rules(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    for event in (
        session.event(HookEventType.PRE_COMPACT, trigger="auto"),
        session.event(HookEventType.SESSION_START, source="compact"),
        session.event(HookEventType.POST_COMPACT, trigger="auto"),
    ):
        assert send(event).decision == "allow"

    assert session.evaluated == []
    assert session.variables["unlocked_tools"] == ["mcp__gobby__call_tool"]
    assert "pre_compact_seen" not in session.variables

    send(session.event(HookEventType.BEFORE_AGENT, prompt="continue"))

    assert session.evaluated == [HookEventType.BEFORE_AGENT]
    assert session.variables["unlocked_tools"] == ["mcp__gobby__call_tool"]
    assert "pre_compact_seen" not in session.variables


def test_parent_auto_compaction_replays_through_the_rules(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(HookEventType.PRE_COMPACT, trigger="auto"))
    assert session.evaluated == []
    assert "pre_compact_seen" not in session.variables

    session.parent_boundary("boundary-1")
    send(session.event(HookEventType.BEFORE_AGENT, prompt="continue"))

    assert session.evaluated == [HookEventType.PRE_COMPACT, HookEventType.BEFORE_AGENT]
    assert session.variables["pre_compact_seen"] is True


def test_parent_auto_compaction_replays_its_whole_sequence_through_the_rules(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    # The parent's own auto-compaction can also emit SessionStart(compact) and PostCompact
    # after its boundary; all three belong to the same compaction and must all reach the rules.
    send(session.event(HookEventType.PRE_COMPACT, trigger="auto"))
    session.parent_boundary("boundary-1")
    send(session.event(HookEventType.SESSION_START, source="compact"))
    send(session.event(HookEventType.POST_COMPACT, trigger="auto"))
    send(session.event(HookEventType.BEFORE_AGENT, prompt="continue"))

    assert session.evaluated == [
        HookEventType.PRE_COMPACT,
        HookEventType.SESSION_START,
        HookEventType.POST_COMPACT,
        HookEventType.BEFORE_AGENT,
    ]
    assert session.variables["unlocked_tools"] == []
    assert session.variables["pre_compact_seen"] is True


def test_a_verified_parent_boundary_is_not_replayed_again(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(HookEventType.PRE_COMPACT, trigger="auto"))
    session.parent_boundary("boundary-1")
    send(session.event(HookEventType.BEFORE_AGENT, prompt="first"))
    session.variables.pop("pre_compact_seen")

    send(session.event(HookEventType.PRE_COMPACT, trigger="auto"))
    send(session.event(HookEventType.BEFORE_AGENT, prompt="second"))

    assert session.evaluated == [
        HookEventType.PRE_COMPACT,
        HookEventType.BEFORE_AGENT,
        HookEventType.BEFORE_AGENT,
    ]
    assert "pre_compact_seen" not in session.variables


def test_parent_manual_compaction_still_resets_after_a_subagent_batch(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(HookEventType.PRE_COMPACT, trigger="auto"))
    send(session.event(HookEventType.SESSION_START, source="compact"))
    send(session.event(HookEventType.BEFORE_AGENT, prompt="subagent batch settles"))

    send(session.event(HookEventType.PRE_COMPACT, trigger="manual"))
    send(session.event(HookEventType.SESSION_START, source="compact"))

    assert session.variables["unlocked_tools"] == []
    assert session.variables["pre_compact_seen"] is True
