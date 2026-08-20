from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.agents.attention_metadata import AttentionMetadataStore
from gobby.agents.attention_tracker import AgentAttentionTracker
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.servers.routes.attention import create_attention_router
from gobby.servers.websocket.broadcast import BroadcastMixin
from gobby.storage.agents import AgentRun
from gobby.storage.attention import AttentionOrderingCoordinator, AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase

from .detection_test_support import BundledDetectionRegistry
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal


@dataclass
class _Clock:
    elapsed: float = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def wall(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC) + timedelta(seconds=self.elapsed)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


def _event(
    event_type: HookEventType,
    *,
    data: dict[str, object] | None = None,
    session_id: str = "session-1",
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="external-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata={"_platform_session_id": session_id},
    )


def _run() -> AgentRun:
    now = datetime.now(UTC)
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        child_session_id="session-1",
        task_id=None,
        status="running",
        provider="claude",
        model=None,
        prompt="test",
        terminal_id="gobby-agent-run-1",
        created_at=now,
        updated_at=now,
    )


async def _run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


def test_expires_at_contract(temp_db: HubDatabase) -> None:
    clock = _Clock()
    manager = AttentionStateManager(temp_db, epoch="epoch-1")
    store = AttentionMetadataStore(
        manager.ordering,
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
    )

    store.set("session:1", "compacting", 10_000)
    clock.advance(4)

    session = SimpleNamespace(
        id="1",
        status="active",
        source="codex",
        model="gpt-5",
        terminal_context={"tmux_pane": "%1"},
        updated_at=clock.wall(),
    )
    server = SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=manager,
            attention_metadata_store=store,
            agent_lifecycle_monitor=None,
            detection_registry=None,
            session_manager=SimpleNamespace(list=lambda **_kwargs: [session]),
            task_manager=SimpleNamespace(get_task=lambda _task_id: None),
            agent_runner=None,
            database=temp_db,
            run_db=_run_db,
        )
    )
    app = FastAPI()
    app.include_router(create_attention_router(server))
    response = TestClient(app).get("/api/attention/roster")
    entry = response.json()["entries"][0]

    assert response.status_code == 200
    assert response.json()["epoch"] == "epoch-1"
    assert response.json()["seq"] == 1
    assert entry["metadata"] == {
        "text": "compacting",
        "expires_at": "2026-01-02T03:04:15+00:00",
    }


def test_expiry_without_followup_event() -> None:
    clock = _Clock()
    events: list[dict[str, object]] = []
    store = AttentionMetadataStore(
        AttentionOrderingCoordinator(epoch="epoch-1"),
        event_publisher=events.append,
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
    )

    store.set("run:1", "retrying provider", 1_000)
    assert len(events) == 1

    clock.advance(1.001)
    assert store.get("run:1") is None
    assert store.snapshot() == {}
    assert len(events) == 1


def test_clear_removes_entry_and_publishes_cursor_ordered_tombstone() -> None:
    events: list[dict[str, object]] = []
    store = AttentionMetadataStore(
        AttentionOrderingCoordinator(epoch="epoch-1"),
        event_publisher=events.append,
    )
    store.set("run:1", "retrying provider", 1_000)

    assert store.clear("run:1") is True
    assert store.get("run:1") is None
    assert events[-1] == {
        "entry_id": "run:1",
        "epoch": "epoch-1",
        "seq": 2,
        "metadata": None,
    }
    assert store.clear("run:1") is False
    assert len(events) == 2


@pytest.mark.asyncio
async def test_live_emit_and_validation() -> None:
    clock = _Clock()
    events: list[dict[str, object]] = []
    ordering = AttentionOrderingCoordinator(epoch="epoch-live")
    store = AttentionMetadataStore(
        ordering,
        event_publisher=events.append,
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
    )
    server = SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=None,
            attention_metadata_store=store,
            agent_lifecycle_monitor=None,
            detection_registry=None,
        )
    )
    app = FastAPI()
    app.include_router(create_attention_router(server))
    client = TestClient(app)

    response = client.post(
        "/api/attention/run:api/metadata",
        json={"text": "indexing", "ttl_ms": 5_000},
    )
    assert response.status_code == 200
    assert events[-1] == {
        "entry_id": "run:api",
        "epoch": "epoch-live",
        "seq": 1,
        "metadata": {
            "text": "indexing",
            "expires_at": "2026-01-02T03:04:10+00:00",
        },
    }

    broadcaster = BroadcastMixin()
    broadcaster.clients = {}
    broadcaster.configure_attention_metadata(store)
    broadcaster.broadcast = AsyncMock()  # type: ignore[method-assign]
    await broadcaster.broadcast_agent_event(
        "status_changed",
        "api",
        "parent-1",
        entry_id="run:api",
    )
    live_message = broadcaster.broadcast.await_args.args[0]
    assert live_message["metadata"] == events[-1]["metadata"]

    for payload in (
        {"text": "x" * 121, "ttl_ms": 1_000},
        {"text": "bad\ntext", "ttl_ms": 1_000},
        {"text": "bad ttl", "ttl_ms": 0},
        {"text": "bad ttl", "ttl_ms": 1.5},
        {"text": "bad ttl", "ttl_ms": 600_001},
    ):
        assert client.post("/api/attention/run:api/metadata", json=payload).status_code == 422
    assert ordering.seq == 1

    hook_logger = MagicMock()
    handlers = EventHandlers(attention_metadata_store=store, logger=hook_logger)
    handlers.handle_pre_compact(_event(HookEventType.PRE_COMPACT))
    assert store.get("session:session-1")["text"] == "compacting"

    handlers.handle_after_agent(
        _event(
            HookEventType.AFTER_AGENT,
            data={"attention_metadata": {"text": "writing tests", "ttl_ms": 5_000}},
        )
    )
    assert store.get("session:session-1")["text"] == "writing tests"

    prior_seq = ordering.seq
    handlers.handle_after_agent(
        _event(
            HookEventType.AFTER_AGENT,
            data={"attention_metadata": {"text": "invalid\ntext", "ttl_ms": 5_000}},
        )
    )
    assert ordering.seq == prior_seq
    hook_logger.warning.assert_called()


@pytest.mark.asyncio
async def test_attention_tracker_sets_stall_and_dismissal_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BundledDetectionRegistry()
    prompt_detector = PromptDetector(registry, "claude")
    stall_classifier = StallClassifier(registry, "claude")
    attention_manager = MagicMock()
    attention_manager.transition_async = AsyncMock()
    store = AttentionMetadataStore(AttentionOrderingCoordinator(epoch="epoch-idle"))
    config = MagicMock()
    config.auto_enter_approval_prompts = True
    tracker = AgentAttentionTracker(
        run_db=_run_db,
        prompt_detector=prompt_detector,
        stall_classifier=stall_classifier,
        tmux_config=config,
        attention_manager=attention_manager,
        attention_metadata_store=store,
    )
    run = _run()

    monkeypatch.setattr("gobby.agents.stall_classifier._MIN_CHECK_INTERVAL_SECONDS", 0)
    await tracker.sync(run, "503 service unavailable")
    await tracker.sync(run, "503 service unavailable")
    assert store.get("run:run-1")["text"] == "retrying provider"
    await tracker.clear(run)
    assert store.get("run:run-1") is None

    approval = "Permission required: press Enter to approve this command"
    prompt_detector.for_provider(run.provider).mark_approval_prompt_dismissed(run.id, approval)
    await tracker.sync(run, approval)
    assert store.get("run:run-1")["text"] == "needs attention"
    await tracker.clear_after_injection(run)
    assert store.get("run:run-1") is None
