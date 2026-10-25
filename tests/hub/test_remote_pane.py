"""Node pane state on the hub: revision store, attention application, injection clears."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents.attention_tracker import AgentAttentionTracker, AttentionClassification
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.hub import remote_pane
from gobby.hub.node_commands import ReportAgent
from gobby.storage.agents import AgentRun
from gobby.storage.attention import AttentionStateManager, run_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import BundledDetectionRegistry

pytestmark = pytest.mark.unit

MACHINE = "5d0e8a52-1f4b-4c39-9a4e-2b7c6f1d0b01"
OTHER = "5d0e8a52-1f4b-4c39-9a4e-2b7c6f1d0b02"
DIGEST = "ab" * 32
APPROVAL = "Permission required: press Enter to approve this command"
QUESTION = "Choose a response:\n1. Yes / 2. No\n"
CLEAN = "all quiet"
STALL = "503 service unavailable"
_STAMP = datetime(2026, 1, 1, tzinfo=UTC)


async def _run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


def _run(run_id: str) -> AgentRun:
    return AgentRun(
        id=run_id,
        parent_session_id="parent-1",
        child_session_id="session-1",
        provider="claude",
        prompt="test",
        status="running",
        created_at=_STAMP,
        updated_at=_STAMP,
        terminal_id="agent-run-1",
    )


def _manager(db: HubDatabase) -> AttentionStateManager:
    return AttentionStateManager(db, epoch="test-epoch")


def _tracker(manager: AttentionStateManager) -> AgentAttentionTracker:
    registry = BundledDetectionRegistry()
    config = MagicMock()
    config.auto_enter_approval_prompts = False
    return AgentAttentionTracker(
        run_db=_run_db,
        prompt_detector=PromptDetector(registry, "claude"),
        stall_classifier=StallClassifier(registry, "claude"),
        tmux_config=config,
        attention_manager=manager,
    )


def _state(manager: AttentionStateManager, run_id: str) -> tuple[object, ...] | None:
    row = manager.get(run_attention_entry_id(run_id))
    if row is None:
        return None
    return (row.state, row.reason, row.kind, row.fingerprint, row.payload)


def _blocked_state(manager: AttentionStateManager, run_id: str) -> str | None:
    row = manager.get(run_attention_entry_id(run_id))
    return None if row is None else row.state


def _pane(*, attention: dict[str, Any] | None = None, injections: int = 0) -> dict[str, Any]:
    return {
        "attention": attention,
        "injections": injections,
        "composer": {"state": "empty", "line": None},
        "idle": {
            "status": "idle",
            "turn_in_flight": False,
            "unsubmitted_input": False,
            "queued_message_prompt": False,
            "capacity_message": None,
            "quota": None,
        },
        "tail": "",
    }


def _attention(reason: str = "approval", kind: str = "actionable") -> dict[str, Any]:
    return {
        "reason": reason,
        "kind": kind,
        "fingerprint": DIGEST,
        "payload": {
            "kind": reason,
            "excerpt": "Permission required",
            "options": [],
            "fingerprint": DIGEST,
        },
        "dismissed": False,
    }


def _agent(run_id: str, terminal_id: str, pane: dict[str, Any] | None) -> ReportAgent:
    return ReportAgent.model_validate(
        {
            "run_id": run_id,
            "terminal_id": terminal_id,
            "pid": 4242,
            "locator": "pane-1",
            "locator_key": "host-1:pane-1",
            "workspace": None,
            "pane": pane,
            "sandbox": None,
        }
    )


def test_store_follows_report_revisions() -> None:
    store = remote_pane.RemotePaneStore()
    assert store.apply_report(MACHINE, 1, [_agent("run-a", "term-a", _pane())]) == {}
    assert store.get("term-a", MACHINE) is not None

    previous = store.apply_report(MACHINE, 3, [_agent("run-b", "term-b", _pane())])
    assert previous is not None
    assert set(previous) == {"term-a"}
    assert store.get("term-a", MACHINE) is None
    assert store.get("term-b", MACHINE) is not None

    assert store.apply_report(MACHINE, 2, [_agent("run-c", "term-c", _pane())]) is None
    assert store.apply_report(MACHINE, 3, [_agent("run-c", "term-c", _pane())]) is None
    assert store.get("term-c", MACHINE) is None
    assert store.get("term-b", MACHINE) is not None

    assert store.apply_report(OTHER, 1, [_agent("run-d", "term-d", _pane())]) == {}
    other = store.get("term-d", OTHER)
    assert other is not None
    assert other.machine_id == OTHER
    assert store.get("term-b", MACHINE) is not None


def test_store_lists_only_machines_with_accepted_reports() -> None:
    store = remote_pane.RemotePaneStore()
    store.record_tail("term-a", MACHINE, "run-a", "tail")
    assert store.machine_ids() == ()
    assert store.apply_report(MACHINE, 1, [_agent("run-a", "term-a", _pane())]) == {}
    assert store.machine_ids() == (MACHINE,)


def test_store_marks_terminal_stopped() -> None:
    store = remote_pane.RemotePaneStore()
    store.apply_report(MACHINE, 1, [_agent("run-a", "term-a", _pane())])
    store.apply_report(OTHER, 1, [_agent("run-d", "term-d", _pane())])

    store.mark_stopped("term-a", MACHINE)
    store.mark_stopped("term-d", OTHER)
    stopped = store.get("term-a", MACHINE)
    assert stopped is not None
    assert stopped.stopped is True
    other = store.get("term-d", OTHER)
    assert other is not None
    assert other.stopped is True

    store.mark_stopped("term-unknown", MACHINE)
    assert store.get("term-unknown", MACHINE) is None

    store.apply_report(MACHINE, 2, [_agent("run-a", "term-a", _pane())])
    kept = store.get("term-a", MACHINE)
    assert kept is not None
    assert kept.stopped is True

    store.apply_report(MACHINE, 3, [_agent("run-b", "term-a", _pane())])
    restarted = store.get("term-a", MACHINE)
    assert restarted is not None
    assert restarted.stopped is False

    store.apply_report(MACHINE, 4, [])
    assert store.get("term-a", MACHINE) is None


@pytest.mark.asyncio
async def test_node_attention_applies(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(temp_db)
    tracker = _tracker(manager)
    run = _run("run-node")
    entered: list[tuple[object, ...]] = []
    resolved: list[str] = []

    async def record_enter(
        session_id: str | None, reason: str, fingerprint: str, provider: str
    ) -> None:
        entered.append((session_id, reason, fingerprint, provider))

    async def record_resolve(entry_id: str) -> None:
        resolved.append(entry_id)

    monkeypatch.setattr(tracker, "_enter_lifecycle_wait", record_enter)
    monkeypatch.setattr(tracker, "_resolve_lifecycle_wait", record_resolve)

    await tracker.sync(run, APPROVAL)
    local = _state(manager, run.id)
    local_entered = list(entered)
    await tracker.clear(run)
    entered.clear()
    resolved.clear()

    detector = PromptDetector(BundledDetectionRegistry(), "claude").for_provider("claude")
    detected = detector.detect_prompt(APPROVAL)
    assert detected is not None
    block = AttentionClassification(
        reason="approval",
        kind="actionable",
        fingerprint=detected.fingerprint,
        payload=detected.to_payload(),
        dismissed=False,
    )
    await tracker.apply(run, block)
    assert _state(manager, run.id) == local
    assert entered == local_entered

    await tracker.apply(run, None)
    assert _blocked_state(manager, run.id) is None
    assert resolved == [run_attention_entry_id(run.id)]


@pytest.mark.asyncio
async def test_injection_clears_attention(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(temp_db)
    tracker = _tracker(manager)
    run = _run("run-inject")
    monkeypatch.setattr(
        remote_pane,
        "load_reported_run",
        lambda db, machine_id, run_id, terminal_id: run,
    )
    cleared: list[str] = []
    real_clear = tracker.clear_after_injection

    async def record_clear(target: AgentRun) -> None:
        cleared.append(target.id)
        await real_clear(target)

    monkeypatch.setattr(tracker, "clear_after_injection", record_clear)
    store = remote_pane.RemotePaneStore()
    steps = [(1, 0, _attention()), (2, 1, None), (3, 1, _attention())]

    for revision, injections, attention in steps:
        agents = [_agent(run.id, "term-inject", _pane(injections=injections, attention=attention))]
        previous = store.apply_report(MACHINE, revision, agents)
        assert previous is not None
        await remote_pane.apply_pane_state(
            temp_db,
            tracker,
            machine_id=MACHINE,
            previous=previous,
            agents=agents,
            run_db=_run_db,
        )
        if revision == 1:
            assert cleared == []
            assert _blocked_state(manager, run.id) == "blocked"
        if revision == 2:
            assert cleared == [run.id]
            assert _blocked_state(manager, run.id) is None

    assert cleared == [run.id]
    row = manager.get(run_attention_entry_id(run.id))
    assert row is not None
    assert row.state == "blocked"


@pytest.mark.asyncio
async def test_local_sync_unchanged(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.agents.stall_classifier._MIN_CHECK_INTERVAL_SECONDS", 0)
    manager = _manager(temp_db)
    tracker = _tracker(manager)
    synced_run = _run("run-sync")
    split_run = _run("run-split")
    synced: list[tuple[object, ...] | None] = []
    split: list[tuple[object, ...] | None] = []

    for pane in [APPROVAL, CLEAN, QUESTION, CLEAN, STALL, STALL]:
        await tracker.sync(synced_run, pane)
        synced.append(_state(manager, synced_run.id))
        await tracker.apply(split_run, tracker.classify(split_run, pane))
        split.append(_state(manager, split_run.id))

    assert split == synced
    # Recorded from the pre-split tracker at e74de3693b, so the split changes no transition.
    assert [None if state is None else state[:3] for state in synced] == [
        ("blocked", "approval", "actionable"),
        (None, None, None),
        ("blocked", "question", "actionable"),
        (None, None, None),
        (None, None, None),
        ("blocked", "stall", "non_actionable"),
    ]


@pytest.mark.asyncio
async def test_bad_attention_kind_does_not_block_later_agent(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(temp_db)
    tracker = _tracker(manager)
    bad = _run("run-bad")
    good = _run("run-good")
    runs = {bad.id: bad, good.id: good}
    monkeypatch.setattr(
        remote_pane,
        "load_reported_run",
        lambda db, machine_id, run_id, terminal_id: runs.get(run_id),
    )
    agents = [
        _agent(bad.id, "term-bad", _pane(attention=_attention(kind="unknown"))),
        _agent(good.id, "term-good", _pane(attention=_attention())),
    ]

    await remote_pane.apply_pane_state(
        temp_db,
        tracker,
        machine_id=MACHINE,
        previous={},
        agents=agents,
        run_db=_run_db,
    )

    assert _blocked_state(manager, good.id) == "blocked"
    assert _blocked_state(manager, bad.id) is None


@pytest.mark.asyncio
async def test_report_applications_keep_revision_order(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_pane.remote_pane_store.cache_clear()
    manager = _manager(temp_db)
    tracker = _tracker(manager)
    run = _run("run-order")
    monkeypatch.setattr(
        remote_pane,
        "load_reported_run",
        lambda db, machine_id, run_id, terminal_id: run,
    )
    held = asyncio.Event()
    release = asyncio.Event()
    holds = [True]

    async def gated_run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        if holds[0]:
            holds[0] = False
            held.set()
            await release.wait()
        return function(*args, **kwargs)

    older = [_agent(run.id, "term-order", _pane(attention=_attention()))]
    newer = [_agent(run.id, "term-order", _pane(attention=None))]
    first = asyncio.create_task(
        remote_pane.apply_report_panes(
            temp_db, tracker, machine_id=MACHINE, revision=1, agents=older, run_db=gated_run_db
        )
    )
    await held.wait()
    second = asyncio.create_task(
        remote_pane.apply_report_panes(
            temp_db, tracker, machine_id=MACHINE, revision=2, agents=newer, run_db=gated_run_db
        )
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert _blocked_state(manager, run.id) is None


@pytest.fixture
def fresh_pane_store() -> Iterator[None]:
    remote_pane.remote_pane_store.cache_clear()
    yield
    remote_pane.remote_pane_store.cache_clear()


async def _attend(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, *, stopped: bool
) -> tuple[AttentionStateManager, AgentRun]:
    manager = _manager(temp_db)
    tracker = _tracker(manager)
    run = _run("run-stopped")
    agent = _agent(run.id, "term-stopped", _pane(attention=_attention()))
    store = remote_pane.remote_pane_store()
    store.apply_report(MACHINE, 1, [agent])
    if stopped:
        store.mark_stopped("term-stopped", MACHINE)

    async def load_run(_function: Any, *_args: Any, **_kwargs: Any) -> AgentRun:
        return run

    async def ignore(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tracker, "_enter_lifecycle_wait", ignore)
    monkeypatch.setattr(tracker, "_resolve_lifecycle_wait", ignore)
    await remote_pane.apply_pane_state(
        temp_db, tracker, machine_id=MACHINE, previous={}, agents=[agent], run_db=load_run
    )
    return manager, run


@pytest.mark.asyncio
async def test_stopped_terminal_is_not_attended(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, fresh_pane_store: None
) -> None:
    manager, run = await _attend(temp_db, monkeypatch, stopped=True)

    assert _blocked_state(manager, run.id) is None


@pytest.mark.asyncio
async def test_live_terminal_is_attended(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, fresh_pane_store: None
) -> None:
    manager, run = await _attend(temp_db, monkeypatch, stopped=False)

    assert _blocked_state(manager, run.id) is not None


def test_store_answers_and_stops_only_for_the_hosting_machine() -> None:
    store = remote_pane.RemotePaneStore()
    store.apply_report(OTHER, 1, [_agent("run-x", "term-a", _pane())])
    store.apply_report(MACHINE, 1, [_agent("run-a", "term-a", _pane())])

    owner = store.get("term-a", MACHINE)
    assert owner is not None
    assert owner.machine_id == MACHINE
    assert owner.run_id == "run-a"

    store.mark_stopped("term-a", MACHINE)
    stopped = store.get("term-a", MACHINE)
    assert stopped is not None
    assert stopped.stopped is True
    forged = store.get("term-a", OTHER)
    assert forged is not None
    assert forged.stopped is False
    assert store.get("term-a", "unknown-machine") is None


def test_store_reads_wait_for_a_writer() -> None:
    store = remote_pane.RemotePaneStore()
    store.apply_report(MACHINE, 1, [_agent("run-a", "term-a", _pane())])
    read: list[remote_pane.RemotePane | None] = []
    reader = threading.Thread(target=lambda: read.append(store.get("term-a", MACHINE)))

    with store._lock:
        reader.start()
        reader.join(timeout=0.2)
        blocked = reader.is_alive()
    reader.join(timeout=5)

    assert blocked is True
    assert not reader.is_alive()
    assert read[0] is not None
    assert read[0].run_id == "run-a"
