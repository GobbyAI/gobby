"""Claude compaction hooks through HookManager and the real rule engine (#25683).

A subagent's auto-compaction shares the parent's session id and transcript, so its hooks reach
HookManager with no agent_id. They reach the parent's rules only when the parent's own
transcript gained a compact_boundary. The parent's manual compaction and clear run immediately.
The rules here are synthetic; no bundled definition is named.
"""

from __future__ import annotations

import asyncio
import functools
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
CONTEXT_MARKER = "synthetic-session-start-context"
PRE_CONTEXT_MARKER = "synthetic-pre-compact-context"
PRE = HookEventType.PRE_COMPACT
START = HookEventType.SESSION_START
POST = HookEventType.POST_COMPACT
BEFORE = HookEventType.BEFORE_AGENT

_HOOK_METHODS = (
    "_handle_internal",
    "_run_replays",
    "_handle_routed",
    "_handle_internal_async",
    "_run_replays_async",
    "_handle_routed_async",
)

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
        "reset-on-clear",
        {
            "event": "session_start",
            "when": "event.data.get('source') in ['clear']",
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
    (
        "inject-on-compact-start",
        {
            "event": "session_start",
            "when": "event.data.get('source') in ['compact']",
            "priority": 20,
            "effects": [{"type": "inject_context", "template": CONTEXT_MARKER}],
        },
    ),
    (
        "inject-on-pre-compact",
        {
            "event": "pre_compact",
            "priority": 20,
            "effects": [{"type": "inject_context", "template": PRE_CONTEXT_MARKER}],
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


def _context(response: HookResponse) -> str:
    return response.context or ""


class _Session:
    """HookManager wired to the real rule engine; only the daemon gate and handler stage are stand-ins."""

    def __init__(self, db: HubDatabase, transcript: Path) -> None:
        self.transcript = transcript
        self.engine = _engine(db)
        self.variables: dict[str, Any] = {"unlocked_tools": ["mcp__gobby__call_tool"]}
        self.evaluated: list[HookEvent] = []
        self.manager = MagicMock()
        self.manager._compaction_ownership = CompactionOwnership()
        self.manager._handle_after_daemon_ready = self._evaluate
        for name in _HOOK_METHODS:
            setattr(self.manager, name, functools.partial(getattr(HookManager, name), self.manager))

    @property
    def kinds(self) -> list[HookEventType]:
        return [event.event_type for event in self.evaluated]

    def _evaluate(self, event: HookEvent) -> HookResponse:
        self.evaluated.append(event)
        return asyncio.run(
            self.engine.evaluate(event, session_id=SESSION_ID, variables=self.variables)
        )

    def send_sync(self, event: HookEvent) -> HookResponse:
        return HookManager._handle_internal(self.manager, event)

    async def send_async(self, event: HookEvent) -> HookResponse:
        return await HookManager._handle_internal_async(self.manager, event)

    def event(self, event_type: HookEventType, **data: Any) -> HookEvent:
        return _event(event_type, data, str(self.transcript))

    def parent_boundary(self, uuid: str, at: datetime | None = None) -> None:
        record = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": uuid,
            "timestamp": _stamp(at or datetime.now(UTC)),
            "compactMetadata": {"trigger": "auto"},
        }
        with self.transcript.open("ab") as handle:
            handle.write(_transcript_line(record))

    def subagent_boundary(self, uuid: str, at: datetime) -> None:
        directory = self.transcript.with_suffix("") / "subagents"
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": uuid,
            "timestamp": _stamp(at),
            "agentId": "agent-1",
            "isSidechain": True,
            "compactMetadata": {"trigger": "auto"},
        }
        with (directory / "agent-1.jsonl").open("ab") as handle:
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
    assert send(session.event(PRE, trigger="manual")).decision == "allow"
    assert session.variables.get("pre_compact_seen") is True

    send(session.event(START, source="compact"))

    assert session.variables["unlocked_tools"] == []
    assert session.kinds == [PRE, START]


def test_parent_clear_still_resets_through_the_rules(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(START, source="clear"))

    assert session.kinds == [START]
    assert session.variables["unlocked_tools"] == []


def test_subagent_only_auto_compaction_never_reaches_the_rules(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    # A subagent boundary exists, but the parent's transcript gained none.
    session.subagent_boundary("subagent-boundary-1", datetime.now(UTC))
    for event in (
        session.event(PRE, trigger="auto"),
        session.event(START, source="compact"),
        session.event(POST, trigger="auto"),
    ):
        assert send(event).decision == "allow"

    response = send(session.event(BEFORE, prompt="continue"))

    assert session.kinds == [BEFORE]
    assert session.variables["unlocked_tools"] == ["mcp__gobby__call_tool"]
    assert "pre_compact_seen" not in session.variables
    assert CONTEXT_MARKER not in _context(response)


def test_parent_auto_compaction_replays_through_the_rules(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(PRE, trigger="auto"))
    assert session.kinds == []
    assert "pre_compact_seen" not in session.variables

    session.parent_boundary("boundary-1")
    send(session.event(BEFORE, prompt="continue"))

    assert session.kinds == [PRE, BEFORE]
    assert session.variables["pre_compact_seen"] is True


def test_parent_auto_compaction_replays_its_whole_sequence_and_returns_the_reset_context(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(PRE, trigger="auto"))
    session.parent_boundary("boundary-1")
    send(session.event(START, source="compact"))
    send(session.event(POST, trigger="auto"))

    response = send(session.event(BEFORE, prompt="continue"))

    assert session.kinds == [PRE, START, POST, BEFORE]
    assert session.variables["unlocked_tools"] == []
    assert session.variables["pre_compact_seen"] is True
    assert CONTEXT_MARKER in _context(response)
    # Only the SessionStart context reaches the settling response; PreCompact's does not.
    assert PRE_CONTEXT_MARKER not in _context(response)


def test_interleaved_subagent_and_parent_compactions_settle_once(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    now = datetime.now(UTC)
    send(session.event(PRE, trigger="auto"))
    session.subagent_boundary("subagent-boundary-1", now)
    send(session.event(PRE, trigger="auto"))
    send(session.event(START, source="compact"))
    send(session.event(POST, trigger="auto"))
    session.parent_boundary("boundary-1", now)
    send(session.event(START, source="compact"))
    send(session.event(POST, trigger="auto"))

    response = send(session.event(BEFORE, prompt="continue"))

    assert session.kinds == [PRE, START, POST, BEFORE]
    assert session.variables["unlocked_tools"] == []
    assert CONTEXT_MARKER in _context(response)


@pytest.mark.parametrize("subagent_first", [True, False], ids=["subagent-first", "parent-first"])
def test_same_millisecond_boundaries_settle_the_same_way(
    session: _Session,
    send: Callable[[HookEvent], HookResponse],
    subagent_first: bool,
) -> None:
    at = datetime.now(UTC)
    send(session.event(PRE, trigger="auto"))
    if subagent_first:
        session.subagent_boundary("subagent-boundary-1", at)
        session.parent_boundary("boundary-1", at)
    else:
        session.parent_boundary("boundary-1", at)
        session.subagent_boundary("subagent-boundary-1", at)
    send(session.event(START, source="compact"))
    send(session.event(POST, trigger="auto"))

    response = send(session.event(BEFORE, prompt="continue"))

    assert session.kinds == [PRE, START, POST, BEFORE]
    assert CONTEXT_MARKER in _context(response)


def test_overlapping_auto_pre_compacts_settle_once_with_the_latest_payload(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(PRE, trigger="auto", marker="first"))
    send(session.event(PRE, trigger="auto", marker="second"))
    session.parent_boundary("boundary-1")
    send(session.event(START, source="compact"))

    send(session.event(BEFORE, prompt="continue"))

    assert session.kinds == [PRE, START, BEFORE]
    assert session.evaluated[0].data["marker"] == "second"
    assert session.variables["unlocked_tools"] == []


def test_boundaries_written_before_the_buffer_began_are_not_replayed(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    session.parent_boundary("boundary-0")
    send(session.event(PRE, trigger="auto"))
    send(session.event(START, source="compact"))
    send(session.event(BEFORE, prompt="subagent-only"))

    assert session.kinds == [BEFORE]
    assert session.variables["unlocked_tools"] == ["mcp__gobby__call_tool"]

    session.parent_boundary("boundary-1")
    send(session.event(START, source="compact"))
    send(session.event(BEFORE, prompt="parent-compacted"))

    assert session.kinds == [BEFORE, START, BEFORE]
    assert session.variables["unlocked_tools"] == []


def test_a_verified_parent_boundary_is_not_replayed_again(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(PRE, trigger="auto"))
    session.parent_boundary("boundary-1")
    send(session.event(BEFORE, prompt="first"))
    session.variables.pop("pre_compact_seen")

    send(session.event(PRE, trigger="auto"))
    send(session.event(BEFORE, prompt="second"))

    assert session.kinds == [PRE, BEFORE, BEFORE]
    assert "pre_compact_seen" not in session.variables


def test_parent_manual_compaction_still_resets_after_a_subagent_batch(
    session: _Session, send: Callable[[HookEvent], HookResponse]
) -> None:
    send(session.event(PRE, trigger="auto"))
    send(session.event(START, source="compact"))
    send(session.event(BEFORE, prompt="subagent batch settles"))

    send(session.event(PRE, trigger="manual"))
    send(session.event(START, source="compact"))

    assert session.variables["unlocked_tools"] == []
    assert session.variables["pre_compact_seen"] is True
