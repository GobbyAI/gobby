"""Acceptance tests for the attention roster and ordering cursor."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from gobby.agents.prompt_detector import PromptDetector
from gobby.servers.http import HTTPServer
from gobby.servers.routes.attention import AttentionAnswer, AttentionPane, create_attention_router
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.attention import (
    AttentionRosterRow,
    AttentionRosterTerminal,
    AttentionState,
    AttentionStateManager,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
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
    capability_resolver: Any | None = None,
) -> SimpleNamespace:
    live_sessions = sessions or []
    config = SimpleNamespace(tmux=SimpleNamespace(socket_path="/tmp/gobby.sock"))
    return SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=manager,
            provider_capability_resolver=capability_resolver,
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


def _roster_run(
    run: Any,
    *,
    terminal: AttentionRosterTerminal | None = None,
    task_ref: str | None = None,
    task_stage: str | None = None,
) -> AttentionRosterRow:
    return AttentionRosterRow(
        kind="run",
        source_id=run.id,
        session_id=run.child_session_id,
        lifecycle_status=run.status,
        task_id=run.task_id,
        task_ref=task_ref,
        task_stage=task_stage,
        provider=run.provider,
        model=run.model,
        pid=run.pid,
        updated_at=run.updated_at,
        terminal_context={},
        terminal_id=run.terminal_id,
        terminal=terminal,
    )


def _roster_session(session: Any) -> AttentionRosterRow:
    return AttentionRosterRow(
        kind="session",
        source_id=session.id,
        session_id=session.id,
        lifecycle_status=session.status,
        task_id=None,
        task_ref=None,
        task_stage=None,
        provider=session.source,
        model=session.model,
        pid=None,
        updated_at=session.updated_at,
        terminal_context=session.terminal_context,
        terminal_id=None,
        terminal=None,
    )


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

    injected: list[AttentionAnswer] = []

    async def pane(_state: AttentionState) -> AttentionPane:
        async def capture() -> str:
            return QUESTION_PROMPT

        return AttentionPane(target="%42", tmux_cmd=("tmux",), capture=capture)

    async def inject(_pane: AttentionPane, answer: AttentionAnswer) -> None:
        injected.append(answer)

    server = _server(temp_db, manager, [session])
    terminal = AttentionRosterTerminal(
        id="term-run-2",
        backend="tmux",
        state="orphaned",
        machine_id=require_machine_id(),
        host_epoch=None,
        session_name="agent-run-2",
        locator={
            "socket_path": "/tmp/gobby.sock",
            "pane_id": "%1",
            "server_pid": 123,
            "server_start_time": 456,
        },
    )
    monkeypatch.setattr(
        manager,
        "load_roster_rows",
        lambda *_args, **_kwargs: [
            _roster_run(
                run,
                terminal=terminal,
                task_ref="#42",
                task_stage="development",
            ),
            _roster_session(session),
        ],
    )
    server.services.terminal_manager = SimpleNamespace()
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
    assert run_entry["terminal"]["state"] == "orphaned"
    assert seen.status_code == 200 and responded.status_code == 200
    assert injected[-1].option == 1


def test_roster_spells_the_model_as_its_provider_prints_it(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AttentionStateManager(temp_db, epoch="model-names")
    session = SimpleNamespace(
        id="session-named",
        status="active",
        source="codex",
        model="gpt-5",
        # A roster row needs a pane or a terminal block; the tmux pane is the cheaper one.
        terminal_context={"tmux_pane": "%7"},
        updated_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    run = SimpleNamespace(
        id="run-named",
        child_session_id="agent-session-named",
        status="running",
        task_id=None,
        provider="claude",
        model="sonnet",
        terminal_id=None,
        pid=None,
        updated_at=datetime(2026, 7, 21, 1, tzinfo=UTC),
    )

    monkeypatch.setattr(
        manager,
        "load_roster_rows",
        lambda *_args, **_kwargs: [_roster_run(run), _roster_session(session)],
    )
    catalog = {("codex", "gpt-5"): "GPT-5", ("claude", "sonnet"): "Claude Sonnet 4.5"}
    resolver = SimpleNamespace(
        find_model=lambda provider, model: (
            None
            if (provider, model) not in catalog
            else SimpleNamespace(display_name=catalog[(provider, model)])
        )
    )

    with _client(_server(temp_db, manager, [session], resolver)) as client:
        named = client.get("/api/attention/roster")
    with _client(_server(temp_db, manager, [session])) as client:
        unnamed = client.get("/api/attention/roster")

    assert (named.status_code, unnamed.status_code) == (200, 200)
    spelled = {entry["entry_id"]: entry["model_display_name"] for entry in named.json()["entries"]}
    assert spelled == {"session:session-named": "GPT-5", "run:run-named": "Claude Sonnet 4.5"}
    # Without a capability catalog the roster carries the raw model and no display name.
    bare = {entry["entry_id"]: entry for entry in unnamed.json()["entries"]}
    assert bare["session:session-named"]["model"] == "gpt-5"
    assert {entry["model_display_name"] for entry in bare.values()} == {None}


def test_roster_terminal_block(temp_db: HubDatabase) -> None:
    manager = AttentionStateManager(temp_db, epoch="terminal-block")
    server = _server(temp_db, manager)
    with _client(server) as client:
        roster = client.get("/api/attention/roster")
    assert roster.status_code == 200
    for entry in roster.json()["entries"]:
        assert "terminal" in entry


def test_roster_cold_path_is_bounded_and_cursor_invalidates_cache(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = AttentionStateManager(temp_db, epoch="bounded-cache")
    runs = [
        SimpleNamespace(
            id=f"run-{index}",
            child_session_id=f"session-{index}",
            status="running",
            task_id=f"task-{index}",
            provider="codex",
            model="gpt-5",
            terminal_id=None,
            pid=None,
            updated_at=datetime(2026, 9, 21, tzinfo=UTC),
        )
        for index in range(50)
    ]
    rows = [
        _roster_run(run, task_ref=f"#{index}", task_stage="development")
        for index, run in enumerate(runs)
    ]
    monkeypatch.setattr(manager, "load_roster_rows", lambda *_args, **_kwargs: rows)
    db_jobs = 0

    async def counted_run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal db_jobs
        db_jobs += 1
        return function(*args, **kwargs)

    server = _server(temp_db, manager)
    server.services.run_db = counted_run_db
    caplog.set_level(logging.DEBUG, logger="gobby.servers.routes.attention")

    with _client(server) as client:
        cold = client.get("/api/attention/roster")
        warm = client.get("/api/attention/roster")
        state = _open(manager)
        invalidated = client.get("/api/attention/roster")
        manager.ordering.epoch = "bounded-cache-next"
        epoch_invalidated = client.get("/api/attention/roster")

    assert (
        cold.status_code
        == warm.status_code
        == invalidated.status_code
        == epoch_invalidated.status_code
        == 200
    )
    assert len(cold.json()["entries"]) == 50
    assert db_jobs == 6
    assert epoch_invalidated.json()["epoch"] == "bounded-cache-next"
    invalidated_entries = {entry["entry_id"]: entry for entry in invalidated.json()["entries"]}
    assert invalidated_entries["run:run-1"]["attention"]["attention_id"] == state.attention_id
    messages = [record.getMessage() for record in caplog.records]
    assert any("cache_hit=False" in message and "query_ms=" in message for message in messages)
    assert any(
        "cache_hit=True" in message and "executor_wait_ms=" in message for message in messages
    )


@pytest.mark.asyncio
async def test_warm_roster_cache_check_uses_attention_ordering_lock(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingLock:
        def __init__(self) -> None:
            self.delegate = asyncio.Lock()
            self.entries = 0

        async def __aenter__(self) -> None:
            await self.delegate.acquire()
            self.entries += 1

        async def __aexit__(self, *_args: object) -> None:
            self.delegate.release()

    manager = AttentionStateManager(temp_db, epoch="ordered-cache")
    monkeypatch.setattr(manager, "load_roster_rows", lambda *_args, **_kwargs: [])
    tracking_lock = TrackingLock()
    monkeypatch.setattr(manager.ordering, "_lock", tracking_lock)
    server = _server(temp_db, manager)
    app = FastAPI()
    app.include_router(create_attention_router(cast(HTTPServer, server)))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cold = await client.get("/api/attention/roster")
        tracking_lock.entries = 0
        warm = await client.get("/api/attention/roster")

    assert cold.status_code == warm.status_code == 200
    assert tracking_lock.entries == 1


def test_bounded_roster_query_joins_task_payload(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: Any,
) -> None:
    session = session_manager.register(
        external_id="attention-roster-query-parent",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Roster query task",
        validation_criteria="The joined roster query returns this task payload.",
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        provider="codex",
        model="gpt-5",
        prompt="test",
        task_id=task.id,
    )

    rows = AttentionStateManager(temp_db).load_roster_rows(
        require_machine_id(),
        live_session_statuses=(),
    )

    row = next(item for item in rows if item.source_id == run.id)
    assert row.task_id == task.id
    assert row.task_ref == f"#{task.seq_num}"
    assert row.task_stage is None


@pytest.mark.asyncio
async def test_roster_p95_stays_below_deadline_with_shared_executor_load(
    temp_db: HubDatabase,
) -> None:
    manager = AttentionStateManager(temp_db, epoch="loaded-roster")
    server = _server(temp_db, manager)
    executor = ThreadPoolExecutor(max_workers=4)
    release = threading.Event()
    entered = [threading.Event() for _ in range(3)]

    def concurrent_db_work(ready: threading.Event) -> None:
        ready.set()
        while not release.is_set():
            temp_db.fetchone("SELECT 1")

    async def executor_run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, partial(function, *args, **kwargs))

    server.services.run_db = executor_run_db
    app = FastAPI()
    app.include_router(create_attention_router(cast(HTTPServer, server)))
    loop = asyncio.get_running_loop()
    blockers = [loop.run_in_executor(executor, concurrent_db_work, ready) for ready in entered]

    try:
        assert await asyncio.to_thread(lambda: all(ready.wait(2) for ready in entered))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            async def fetch() -> float:
                started_at = perf_counter()
                response = await client.get("/api/attention/roster")
                assert response.status_code == 200
                return perf_counter() - started_at

            async with asyncio.timeout(5.0):
                latencies = await asyncio.gather(*(fetch() for _ in range(20)))
    finally:
        release.set()
        await asyncio.gather(*blockers)
        executor.shutdown(wait=True)

    p95 = sorted(latencies)[18]
    assert p95 < 1.0, f"roster p95 {p95:.3f}s exceeded the 1s loaded-test budget"
