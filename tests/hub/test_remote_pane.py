"""Node pane state on the hub: revision store, attention application, injection clears."""

from __future__ import annotations

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
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
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
    assert store.get("term-a") is not None

    previous = store.apply_report(MACHINE, 3, [_agent("run-b", "term-b", _pane())])
    assert previous is not None
    assert set(previous) == {"term-a"}
    assert store.get("term-a") is None
    assert store.get("term-b") is not None

    assert store.apply_report(MACHINE, 2, [_agent("run-c", "term-c", _pane())]) is None
    assert store.apply_report(MACHINE, 3, [_agent("run-c", "term-c", _pane())]) is None
    assert store.get("term-c") is None
    assert store.get("term-b") is not None

    assert store.apply_report(OTHER, 1, [_agent("run-d", "term-d", _pane())]) == {}
    other = store.get("term-d")
    assert other is not None
    assert other.machine_id == OTHER
    assert store.get("term-b") is not None


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
    assert any(state is not None and state[0] == "blocked" for state in synced)
