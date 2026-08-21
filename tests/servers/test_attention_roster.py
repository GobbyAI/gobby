"""Acceptance tests for the attention roster and ordering cursor."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.agents.prompt_detector import PromptDetector
from gobby.servers.routes.attention import AttentionAnswer, AttentionPane, create_attention_router
from gobby.storage.attention import AttentionState, AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = BundledDetectionRegistry()
pytestmark = pytest.mark.unit
QUESTION_PROMPT = "Would you like to continue?\n1. Yes\n2. No\n"


async def _run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


def _open(
    manager: AttentionStateManager,
    entry_id: str = "run:run-1",
    run_id: str | None = "run-1",
    session_id: str = "session-1",
) -> AttentionState:
    prompt = PromptDetector(DETECTION_REGISTRY, "claude").detect_prompt(QUESTION_PROMPT)
    assert prompt is not None
    result = manager.transition(
        entry_id,
        state="blocked",
        run_id=run_id,
        session_id=session_id,
        reason=prompt.kind,
        kind="actionable",
        fingerprint=prompt.fingerprint,
        payload=prompt.to_payload(),
    )
    assert result.current is not None
    return result.current


def _server(
    temp_db: HubDatabase,
    manager: AttentionStateManager,
    sessions: list[Any] | None = None,
) -> SimpleNamespace:
    live_sessions = sessions or []
    config = SimpleNamespace(tmux=SimpleNamespace(socket_path="/tmp/gobby.sock"))
    return SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=manager,
            agent_lifecycle_monitor=SimpleNamespace(
                prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude")
            ),
            session_manager=SimpleNamespace(
                list=lambda **_kwargs: list(live_sessions),
                get=lambda session_id: next(
                    (item for item in live_sessions if item.id == session_id), None
                ),
            ),
            task_manager=SimpleNamespace(get_task=lambda _task_id: None),
            agent_runner=None,
            database=temp_db,
            config=config,
            config_runtime=SimpleNamespace(snapshot=SimpleNamespace(active=config)),
            run_db=_run_db,
        )
    )


def _client(server: SimpleNamespace, **kwargs: Any) -> TestClient:
    app = FastAPI()
    app.include_router(create_attention_router(server, **kwargs))
    return TestClient(app)


def test_ordering_coordinator_no_regression(temp_db: HubDatabase) -> None:
    events: list[dict[str, object]] = []
    errors: list[BaseException] = []
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()
    tracker_lock = threading.Lock()
    active_critical_sections = 0
    max_active_critical_sections = 0

    def enter_critical_section() -> None:
        nonlocal active_critical_sections, max_active_critical_sections
        with tracker_lock:
            active_critical_sections += 1
            max_active_critical_sections = max(
                max_active_critical_sections,
                active_critical_sections,
            )

    def exit_critical_section() -> None:
        nonlocal active_critical_sections
        with tracker_lock:
            active_critical_sections -= 1

    def run_worker(worker: Callable[[], object]) -> None:
        try:
            worker()
        except BaseException as exc:
            errors.append(exc)

    def publish(event: dict[str, object]) -> None:
        enter_critical_section()
        try:
            if event["entry_id"] == "run:first":
                enqueue_entered.set()
                assert release_enqueue.wait(timeout=2)
            events.append(event)
        finally:
            exit_critical_section()

    manager = AttentionStateManager(temp_db, event_publisher=publish, epoch="epoch-a")
    assert isinstance(manager.ordering.lock, asyncio.Lock)
    first = threading.Thread(
        target=lambda: run_worker(lambda: _open(manager, "run:first", "first"))
    )

    def second_transition() -> None:
        _open(manager, "run:second", "second")

    first.start()
    assert enqueue_entered.wait(timeout=2)
    second = threading.Thread(target=lambda: run_worker(second_transition))
    second.start()
    release_enqueue.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert [event["entry_id"] for event in events] == ["run:first", "run:second"]
    assert [event["seq"] for event in events] == [1, 2]

    metadata = {"run:first": {"text": "older"}}
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()

    def snapshot_metadata() -> dict[str, dict[str, str]]:
        enter_critical_section()
        try:
            snapshot_entered.set()
            assert release_snapshot.wait(timeout=2)
            return {key: dict(value) for key, value in metadata.items()}
        finally:
            exit_critical_section()

    snapshots: list[Any] = []
    roster_thread = threading.Thread(
        target=lambda: run_worker(
            lambda: snapshots.append(manager.snapshot(metadata_snapshot=snapshot_metadata))
        )
    )

    def transition_during_snapshot() -> None:
        _open(manager, "run:during-snapshot", "during-snapshot")

    def update_metadata() -> None:
        for text in ("newer", "newest"):
            with manager.ordering.synchronized():
                enter_critical_section()
                try:
                    metadata["run:first"] = {"text": text}
                    manager.ordering.next_seq()
                finally:
                    exit_critical_section()

    roster_thread.start()
    assert snapshot_entered.wait(timeout=2)
    transition_thread = threading.Thread(target=lambda: run_worker(transition_during_snapshot))
    metadata_thread = threading.Thread(target=lambda: run_worker(update_metadata))
    transition_thread.start()
    metadata_thread.start()
    release_snapshot.set()
    roster_thread.join(timeout=2)
    transition_thread.join(timeout=2)
    metadata_thread.join(timeout=2)
    assert not roster_thread.is_alive()
    assert not transition_thread.is_alive()
    assert not metadata_thread.is_alive()
    assert errors == []
    assert max_active_critical_sections == 1
    assert snapshots[0].seq == 2
    assert "run:during-snapshot" not in {state.entry_id for state in snapshots[0].states}
    assert snapshots[0].metadata["run:first"] == {"text": "older"}
    assert manager.ordering.seq == 5
    assert metadata["run:first"] == {"text": "newest"}
    assert AttentionStateManager(temp_db, epoch="epoch-b").epoch != manager.epoch


def test_mark_seen_episode(temp_db: HubDatabase) -> None:
    events: list[dict[str, object]] = []
    manager = AttentionStateManager(temp_db, event_publisher=events.append, epoch="seen")
    state = _open(manager)
    with _client(_server(temp_db, manager)) as client:
        seen = client.post(
            f"/api/attention/{state.entry_id}/seen", json={"attention_id": state.attention_id}
        )
        stale = client.post(
            f"/api/attention/{state.entry_id}/seen", json={"attention_id": "retired"}
        )
    assert seen.status_code == 200
    current = manager.get(state.entry_id)
    assert current is not None and current.seen_at is not None
    assert events[-1]["seen_at"] == current.seen_at
    assert stale.status_code == 409


def test_interactive_entry_end_to_end(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AttentionStateManager(temp_db, epoch="interactive")
    session_id = "interactive-1"
    state = _open(manager, f"session:{session_id}", None, session_id)
    session = SimpleNamespace(
        id=session_id,
        status="active",
        source="codex",
        model="gpt-5",
        terminal_context={
            "tmux_pane": "%42",
            "tmux_session": "interactive-shell",
            "tmux_socket_path": "/tmp/interactive.sock",
            "parent_pid": 4242,
        },
        updated_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    run = SimpleNamespace(
        id="run-2",
        child_session_id="agent-session-2",
        status="running",
        task_id="task-2",
        provider="claude",
        model="sonnet",
        terminal_id="term-run-2",
        pid=4343,
        updated_at=datetime(2026, 7, 21, 1, tzinfo=UTC),
    )

    class RosterRuns:
        def __init__(self, _db: HubDatabase) -> None:
            pass

        def list_active_for_machine(
            self,
            machine_id: str,
            limit: int = 500,
            offset: int = 0,
        ) -> list[Any]:
            del machine_id, limit
            if offset:
                return []
            return [run]

    monkeypatch.setattr("gobby.servers.routes.attention.LocalAgentRunManager", RosterRuns)
    injected: list[AttentionAnswer] = []

    async def pane(_state: AttentionState) -> AttentionPane:
        async def capture() -> str:
            return QUESTION_PROMPT

        return AttentionPane(target="%42", tmux_cmd=("tmux",), capture=capture)

    async def inject(_pane: AttentionPane, answer: AttentionAnswer) -> None:
        injected.append(answer)

    server = _server(temp_db, manager, [session])
    from gobby.storage.terminals import AttachLocator

    row = SimpleNamespace(session_name="agent-run-2", backend="tmux", host_epoch=None, id="term-run-2")
    server.services.terminal_manager = SimpleNamespace(
        get=lambda _tid: row,
        get_live_for_session=lambda _sid: None,
        attach_locator=lambda *_a, **_k: AttachLocator(
            backend="tmux",
            frame_host_epoch="",
            socket_path="/tmp/gobby.sock",
            pane_id="%1",
        ),
    )
    task = SimpleNamespace(
        id="task-2",
        to_brief=lambda: {
            "ref": "#42",
            "state": {"current_stage": {"name": "development"}},
        },
    )
    server.services.task_manager = SimpleNamespace(get_task=lambda _task_id: task)
    with _client(server, pane_resolver=pane, injector=inject) as client:
        roster = client.get("/api/attention/roster")
        seen = client.post(
            f"/api/attention/{state.entry_id}/seen", json={"attention_id": state.attention_id}
        )
        responded = client.post(
            f"/api/attention/{state.entry_id}/respond",
            json={
                "attention_id": state.attention_id,
                "fingerprint": state.fingerprint,
                "answer": {"option": 1},
            },
        )
    assert roster.status_code == 200
    roster_payload = roster.json()
    entries = {entry["entry_id"]: entry for entry in roster_payload["entries"]}
    entry = entries[state.entry_id]
    run_entry = entries["run:run-2"]
    assert roster_payload["seq"] == 1
    assert entry["attention"]["attention_id"] == state.attention_id
    assert entry["tmux"] == {
        "socket_path": "/tmp/interactive.sock",
        "session_name": "interactive-shell",
        "parent_pid": 4242,
    }
    assert run_entry["tmux"]["pane_pid"] == 4343
    assert run_entry["terminal"]["terminal_id"] == "term-run-2"
    assert seen.status_code == 200 and responded.status_code == 200
    assert injected[-1].option == 1


def test_roster_terminal_block(temp_db: HubDatabase) -> None:
    manager = AttentionStateManager(temp_db, epoch="terminal-block")
    server = _server(temp_db, manager)
    with _client(server) as client:
        roster = client.get("/api/attention/roster")
    assert roster.status_code == 200
    for entry in roster.json()["entries"]:
        assert "terminal" in entry
