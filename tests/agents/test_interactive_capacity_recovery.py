"""Capacity failures in externally placed seats have no AgentRun to watchdog."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.runtime import SnapshotMode, SnapshotResult
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.test_lifecycle_monitor import LifecycleRuntime, _fake_terminal_services
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    _append_codex_capacity_turn,
)
from tests.terminals.fakes import runtime_registry


@pytest.fixture
def placed_seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Session, Path, LifecycleRuntime, AttentionStateManager]:
    machine_id = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.storage.terminals.require_machine_id", lambda: machine_id)
    transcript = tmp_path / "placed-codex.jsonl"
    _append_codex_capacity_turn(transcript)
    session = session_manager.register(
        external_id="placed-capacity-seat",
        machine_id=machine_id,
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript),
    )
    TerminalManager(temp_db).upsert_external(
        machine_id=machine_id,
        project_id=sample_project["id"],
        backend="tmux",
        locator={"pane_id": "%capacity"},
        locator_key="tmux:%capacity",
        session_id=session.id,
    )
    attention = AttentionStateManager(temp_db)
    runtime = LifecycleRuntime(
        snapshot_text=(
            "Selected model is at capacity. Please try a different model.\n"
            "────────────────────\n›\n────────────────────\n"
        )
    )
    return session, transcript, runtime, attention


@pytest.mark.asyncio
async def test_external_capacity_failure_is_observable_in_one_poll(
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
) -> None:
    session, _transcript, runtime, attention = placed_seat
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=runtime_registry(runtime),
    )

    await monitor._check_attention_panes(active_runs=[])

    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "paused"
    state = attention.get(session_attention_entry_id(session.id))
    assert state is not None
    assert state.reason == "provider_error"
    assert state.kind == "non_actionable"
    assert runtime.write_log == []

    runtime.snapshot_text = "────────────────────\n›\n────────────────────\n"
    await monitor._check_attention_panes(active_runs=[])
    unchanged = attention.get(session_attention_entry_id(session.id))
    assert unchanged is not None
    assert unchanged.reason == "provider_error"


@pytest.mark.asyncio
async def test_capacity_banner_with_control_bytes_is_detected(
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
) -> None:
    """Control bytes drawn inside the banner do not hide it, as in watchdog recovery."""
    session, _transcript, runtime, attention = placed_seat
    runtime.snapshot_text = runtime.snapshot_text.replace(
        "Selected model", "\x1b]0;codex\x07Selected mo\x0fdel"
    )
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=runtime_registry(runtime),
    )

    await monitor._check_attention_panes(active_runs=[])

    state = attention.get(session_attention_entry_id(session.id))
    assert state is not None
    assert state.reason == "provider_error"
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_external_capacity_retries_are_bounded_and_deduplicated(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
) -> None:
    session, transcript, runtime, attention = placed_seat
    services = _fake_terminal_services(temp_db, runtime)
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=services.registry,
        write_coordinator=services.coordinator,
        max_reprompt_attempts=2,
    )

    await monitor._check_attention_panes(active_runs=[])
    first_writes = list(runtime.write_log)
    assert any(kind == "text" for kind, _payload in first_writes)
    await monitor._check_attention_panes(active_runs=[])
    assert runtime.write_log == first_writes

    _append_codex_capacity_turn(transcript)
    await monitor._check_attention_panes(active_runs=[])
    delivered_writes = list(runtime.write_log)
    assert len(delivered_writes) == 2 * len(first_writes)
    _append_codex_capacity_turn(transcript)
    await monitor._check_attention_panes(active_runs=[])

    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "paused"
    state = attention.get(session_attention_entry_id(session.id))
    assert state is not None
    assert state.reason == "provider_error"
    assert runtime.write_log == delivered_writes


@pytest.mark.asyncio
async def test_model_output_resets_capacity_budget(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
) -> None:
    session, transcript, runtime, attention = placed_seat
    services = _fake_terminal_services(temp_db, runtime)
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=services.registry,
        write_coordinator=services.coordinator,
        max_reprompt_attempts=1,
    )
    await monitor._check_attention_panes(active_runs=[])
    first_writes = list(runtime.write_log)

    _append_codex_capacity_turn(transcript, model_output_payload_type="reasoning")
    await monitor._check_attention_panes(active_runs=[])

    assert len(runtime.write_log) == 2 * len(first_writes)
    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("composer", ["› operator draft", "unreadable frame"])
async def test_capacity_retry_preserves_composer_and_budget(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
    composer: str,
) -> None:
    session, _transcript, runtime, attention = placed_seat
    services = _fake_terminal_services(temp_db, runtime)
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=services.registry,
        write_coordinator=services.coordinator,
        max_reprompt_attempts=1,
    )
    empty_frame = runtime.snapshot_text
    runtime.snapshot_text = empty_frame.replace("›", composer)

    await monitor._check_attention_panes(active_runs=[])
    assert runtime.write_log == []
    blocked = attention.get(session_attention_entry_id(session.id))
    assert blocked is not None
    assert blocked.reason == "stall"
    assert blocked.kind == "non_actionable"
    runtime.snapshot_text = empty_frame
    await monitor._check_attention_panes(active_runs=[])
    assert any(kind == "text" for kind, _payload in runtime.write_log)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_tail", ["malformed", "inflight", "missing-pane"])
async def test_capacity_requires_pane_and_conclusive_transcript(
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
    unsafe_tail: str,
) -> None:
    session, transcript, runtime, attention = placed_seat
    if unsafe_tail == "malformed":
        _append_codex_capacity_turn(transcript, malformed_tail=True)
    elif unsafe_tail == "inflight":
        _append_codex_capacity_turn(transcript, lifecycle_suffix=("task_started",))
    else:
        runtime.snapshot_text = "────────────────────\n›\n────────────────────\n"
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=runtime_registry(runtime),
    )

    await monitor._check_attention_panes(active_runs=[])

    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "active"
    assert runtime.write_log == []
    state = attention.get(session_attention_entry_id(session.id))
    assert state is None or state.reason != "provider_error"


@pytest.mark.asyncio
async def test_failed_delivery_is_bounded_separately_from_successful_retries(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
) -> None:
    session, _transcript, runtime, attention = placed_seat
    runtime.write_failures = [True, True]
    services = _fake_terminal_services(temp_db, runtime)
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=services.registry,
        write_coordinator=services.coordinator,
        max_reprompt_attempts=2,
    )

    await monitor._check_attention_panes(active_runs=[])
    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "active"
    await monitor._check_attention_panes(active_runs=[])

    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "paused"
    assert not any(kind == "text" for kind, _payload in runtime.write_log)


@pytest.mark.asyncio
async def test_capacity_composer_probe_outage_retries_on_next_poll(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
) -> None:
    session, _transcript, runtime, attention = placed_seat
    runtime.snapshot_effects = [runtime.snapshot_text, TimeoutError("probe unavailable")]
    services = _fake_terminal_services(temp_db, runtime)
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=services.registry,
        write_coordinator=services.coordinator,
        max_reprompt_attempts=1,
    )

    await monitor._check_attention_panes(active_runs=[])
    assert runtime.write_log == []
    await monitor._check_attention_panes(active_runs=[])

    assert any(kind == "text" for kind, _payload in runtime.write_log)
    current = session_manager.get(session.id)
    assert current is not None
    assert current.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_failure", [False, True])
@pytest.mark.parametrize("replace_attention", [False, True])
async def test_capacity_recheck_reconciles_preserved_composer_attention(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    placed_seat: tuple[Session, Path, LifecycleRuntime, AttentionStateManager],
    monkeypatch: pytest.MonkeyPatch,
    new_failure: bool,
    replace_attention: bool,
) -> None:
    session, transcript, runtime, attention = placed_seat
    services = _fake_terminal_services(temp_db, runtime)
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention,
        registry=services.registry,
        write_coordinator=services.coordinator,
        max_reprompt_attempts=1,
    )
    empty_frame = runtime.snapshot_text
    runtime.snapshot_text = empty_frame.replace("›", "› operator draft")
    await monitor._check_attention_panes(active_runs=[])
    entry_id = session_attention_entry_id(session.id)
    blocked = attention.get(entry_id)
    assert blocked is not None
    assert blocked.fingerprint is not None
    assert blocked.fingerprint.startswith("capacity:")
    runtime.snapshot_text = empty_frame
    original_snapshot = runtime.snapshot

    async def snapshot_after_progress(
        terminal: Terminal, lines: int = 50, *, mode: SnapshotMode = "text"
    ) -> SnapshotResult:
        if mode == "ansi":
            if new_failure:
                _append_codex_capacity_turn(transcript, model_output_payload_type="reasoning")
            else:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        )
                        + "\n"
                    )
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "type": "response_item",
                                "payload": {"type": "reasoning"},
                            }
                        )
                        + "\n"
                    )
            if replace_attention:
                attention.transition(
                    entry_id,
                    state="blocked",
                    session_id=session.id,
                    reason="stall",
                    kind="non_actionable",
                    fingerprint="operator-attention",
                    payload={"label": "Operator intervention"},
                )
        return await original_snapshot(terminal, lines, mode=mode)

    monkeypatch.setattr(runtime, "snapshot", snapshot_after_progress)
    await monitor._check_attention_panes(active_runs=[])

    assert runtime.write_log == []
    current = attention.get(entry_id)
    assert current is not None
    if replace_attention:
        assert current.fingerprint == "operator-attention"
        assert current.state == "blocked"
    else:
        assert current.state is None
