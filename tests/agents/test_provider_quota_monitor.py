"""Spawned-run provider quota observation and verified fail-fast (plan 4.4).

Pane text is a hint only and reaches the shared capacity service through the 4.1
occurrence contract. Only a fresh, current-source transcript quota error from this
run's own terminal attempt fails the run.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.providers.capacity_service import ProviderCapacityService
from gobby.providers.quota_alerts import HINT_COOLDOWN, AlertEdge
from gobby.providers.usage import (
    AgyUsageReporter,
    ProviderUsageReporter,
    ProviderUsageSnapshot,
    TransientUsageRefreshError,
)
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.provider_capacity import ProviderCapacityStorage
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.terminals.services import TerminalServices
from tests.agents.test_lifecycle_monitor import _pane_text
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    LOCAL_MACHINE_ID,
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.providers.test_capacity_service import _agy_reporter_snapshot

pytestmark = pytest.mark.unit

_BASE = "❯ build\nrunning checks\n> step one\n"
_FORMS: dict[str, str] = {
    "claude": "You've hit your limit · resets 5pm\n",
    "codex": "You've hit your usage limit. Try again at 10:30 AM.\n",
    "grok": "Usage balance exhausted\n",
    "droid": "Standard usage limit reached\n",
    "agy": "Individual quota reached. Resets in 1h\n",
    "qwen": "quota exhausted: daily limit\nwill reset at 9pm\n",
}
_USAGE_ERROR = {
    "message": "You've hit your usage limit. Account-specific reset details.",
    "codex_error_info": "usage_limit_exceeded",
}
_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
_RESET = (_NOW + timedelta(hours=5)).isoformat()


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


class _Sink:
    """Records every edge the shared alert sink receives."""

    def __init__(self) -> None:
        self.edges: list[AlertEdge] = []

    async def __call__(self, edge: AlertEdge) -> None:
        self.edges.append(edge)

    def hints(self) -> list[AlertEdge]:
        return [edge for edge in self.edges if edge.kind == "possible_limit"]


class _Reporter:
    """A reporter whose refresh returns or raises a canned outcome."""

    def __init__(self, outcome: ProviderUsageSnapshot | BaseException) -> None:
        self._outcome = outcome

    @property
    def provider(self) -> str:
        return "agy"

    async def report(self) -> ProviderUsageSnapshot:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _OfflineStorage(ProviderCapacityStorage):
    """Capacity storage whose writes fail while reads still work."""

    def upsert(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("capacity storage offline")


def _service(temp_db: HubDatabase, sink: _Sink) -> ProviderCapacityService:
    return ProviderCapacityService.create_default(
        temp_db, machine_id=LOCAL_MACHINE_ID, alert_sink=sink
    )


def _clocked_service(
    temp_db: HubDatabase, sink: _Sink, now: Callable[[], datetime]
) -> ProviderCapacityService:
    return ProviderCapacityService(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID),
        support_resolvers={},
        alert_sink=sink,
        clock=now,
    )


def _reporter_service(
    storage: ProviderCapacityStorage, sink: _Sink, reporter: ProviderUsageReporter
) -> ProviderCapacityService:
    return ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={},
        alert_sink=sink,
    )


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def _observe_pass(monitor: Any) -> None:
    """One lifecycle pass's pane capture, as the loop runs it before the idle check."""
    await monitor.check_attention_agents(reuse_for_idle=True)
    await _settle()


def _spawned(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    *,
    transcript: Path | None = None,
    child_source: str = "codex",
    session_age_seconds: int = 120,
) -> tuple[Any, AgentRun]:
    return _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=str(uuid.uuid4()),
        transcript_path=transcript,
        child_source=child_source,
        session_age_seconds=session_age_seconds,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_FORMS))
async def test_all_provider_observation_owners(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    provider: str,
) -> None:
    sink = _Sink()
    monitor, _run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source=provider
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))

    with _pane_text(monitor, [_BASE, _BASE + _FORMS[provider]]):
        await _observe_pass(monitor)
        assert sink.hints() == []
        await _observe_pass(monitor)

    assert [edge.provider for edge in sink.hints()] == [provider]


@pytest.mark.asyncio
async def test_agy_refresh_drives_verified_windows_into_shared_sink(temp_db: HubDatabase) -> None:
    sink = _Sink()
    reporter = _Reporter(
        _agy_reporter_snapshot(at=_NOW, windows=(("five_hour", 100.0, _RESET),)),
    )
    service = _reporter_service(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID), sink, reporter
    )

    await service.refresh("agy")
    await _settle()

    assert [edge.provider for edge in sink.edges] == ["agy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["recent_session", "coordination_hold", "standing_lifetime"])
async def test_quota_precedes_idle_eligibility(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    gate: str,
) -> None:
    sink = _Sink()
    transcript = tmp_path / f"gated-{gate}.jsonl"
    _write_codex_lifecycle_transcript(transcript, lifecycle_events=(), age_seconds=120)
    monitor, run = _spawned(
        temp_db,
        session_manager,
        sample_project,
        agent_run_manager,
        transcript=transcript,
        session_age_seconds=1 if gate == "recent_session" else 120,
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))
    assert run.child_session_id is not None
    if gate == "standing_lifetime":
        # A standing seat is interactive with no idle TTL, so ordinary idle rules never apply.
        standing = agent_run_manager.update_resume_metadata(
            run.id, {"execution_mode": "interactive"}
        )
        assert standing is not None
    if gate == "coordination_hold":
        CoordinationWaitManager(temp_db).register(
            run.child_session_id, run.parent_session_id, coordination_key="hold"
        )

    with _pane_text(monitor, [_BASE, _BASE + _FORMS["codex"]]):
        await _observe_pass(monitor)
        await _observe_pass(monitor)
    assert [edge.provider for edge in sink.hints()] == ["codex"]
    held = agent_run_manager.get(run.id)
    assert held is not None and held.status == "running"

    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=_USAGE_ERROR,
        event_timestamp=datetime.now(UTC),
    )
    with _pane_text(monitor, _BASE):
        # The settled turn is the pane this pass observes; the idle pass reuses it.
        await _observe_pass(monitor)
        assert await monitor.check_idle_agents() == 1
        assert await monitor.check_idle_agents() == 0

    failed = agent_run_manager.get(run.id)
    assert failed is not None
    assert failed.status == "error"
    assert failed.terminal_reason == "provider_quota_exhausted"


@pytest.mark.asyncio
async def test_quoted_pane_text_never_fails_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "quoted.jsonl"
    _write_codex_lifecycle_transcript(transcript, lifecycle_events=(), age_seconds=120)
    monitor, run = _spawned(
        temp_db,
        session_manager,
        sample_project,
        agent_run_manager,
        transcript=transcript,
    )
    quoted = _BASE + "> " + _FORMS["codex"]

    with _pane_text(monitor, quoted):
        assert await monitor.check_idle_agents() == 0
        assert await monitor.check_idle_agents() == 0

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.terminal_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("form_line", ["genuine", "quoted"])
async def test_genuine_and_quoted_forms_share_hint_only_classification(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    form_line: str,
) -> None:
    sink = _Sink()
    monitor, run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source="codex"
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))
    line = _FORMS["codex"] if form_line == "genuine" else "> " + _FORMS["codex"]

    with _pane_text(monitor, [_BASE, _BASE + line]):
        await _observe_pass(monitor)
        await _observe_pass(monitor)

    assert [edge.provider for edge in sink.hints()] == ["codex"]
    stored = agent_run_manager.get(run.id)
    assert stored is not None and stored.status == "running"


@pytest.mark.asyncio
async def test_terminal_observation_lifecycle(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    sink = _Sink()
    monitor, run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, session_age_seconds=1
    )
    clock = {"now": _NOW}
    monitor.set_provider_capacity_service(_clocked_service(temp_db, sink, lambda: clock["now"]))
    form = _FORMS["codex"]
    banner = form + _BASE
    second_form = "You've hit your usage limit. Try again at 11:00 AM.\n"
    texts: list[str | BaseException] = [
        _BASE,
        _BASE,
        _BASE + form,
        _BASE + form,
        banner,
        banner + "Resets in 1h 5m\n",
        _BASE + form,
        _BASE + form + second_form,
    ]

    with _pane_text(monitor, texts):
        # First capture, unchanged retained history, and a genuine appended block.
        for expected in (0, 0, 1, 1):
            await _observe_pass(monitor)
            assert len(sink.hints()) == expected
        # Past the 900s cooldown, a redraw with no new block and a static relative
        # reset still never re-alert: the occurrence digests alone suppress them.
        clock["now"] += HINT_COOLDOWN + timedelta(seconds=1)
        for expected in (1, 1):
            await _observe_pass(monitor)
            assert len(sink.hints()) == expected
        # A new terminal attempt generation starts from a baseline capture.
        temp_db.execute(
            "UPDATE terminals SET attempt_generation = attempt_generation + 1 "
            "WHERE agent_run_id = %s",
            (run.id,),
        )
        await _observe_pass(monitor)
        assert len(sink.hints()) == 1
        await _observe_pass(monitor)
        assert len(sink.hints()) == 2


@pytest.mark.asyncio
async def test_observation_leaves_no_pending_quota_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    sink = _Sink()
    monitor, _run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, session_age_seconds=1
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))
    before = set(asyncio.all_tasks())

    with _pane_text(monitor, [_BASE, _BASE + _FORMS["codex"]]):
        await _observe_pass(monitor)
        await _observe_pass(monitor)
    await _settle()

    pending = {task for task in asyncio.all_tasks() - before if not task.done()}
    assert pending == set()
    assert len(sink.hints()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transient_refresh", "unsupported_snapshot"])
async def test_reporter_failures_invent_no_edges(
    temp_db: HubDatabase,
    failure: str,
) -> None:
    sink = _Sink()
    outcome: ProviderUsageSnapshot | BaseException
    if failure == "transient_refresh":
        outcome = TransientUsageRefreshError("agy", "timeout", "usage command timed out")
    else:
        outcome = ProviderUsageSnapshot(
            provider="agy",
            observed_at=_NOW,
            supported=False,
            windows=(),
            raw={},
        )
    service = _reporter_service(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID),
        sink,
        _Reporter(outcome),
    )

    await service.refresh("agy")
    await _settle()

    assert sink.edges == []


@pytest.mark.asyncio
async def test_numeric_window_exhaustion_cannot_fail_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    sink = _Sink()
    service = _reporter_service(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID),
        sink,
        _Reporter(_agy_reporter_snapshot(at=_NOW, windows=(("five_hour", 100.0, _RESET),))),
    )
    await service.refresh("agy")
    monitor, run = _spawned(
        temp_db,
        session_manager,
        sample_project,
        agent_run_manager,
        child_source="agy",
    )

    with _pane_text(monitor, _BASE):
        assert await monitor.check_idle_agents() == 0

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "running"


@pytest.mark.asyncio
async def test_storage_failure_keeps_verified_quota_failure(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "storage-failure.jsonl"
    monitor, run = _spawned(
        temp_db,
        session_manager,
        sample_project,
        agent_run_manager,
        transcript=transcript,
        session_age_seconds=1,
    )
    # Written after the run exists: the error belongs to this run's own attempt.
    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=_USAGE_ERROR,
        event_timestamp=datetime.now(UTC),
    )
    sink = _Sink()
    monitor.set_provider_capacity_service(
        ProviderCapacityService(
            _OfflineStorage(temp_db, machine_id=LOCAL_MACHINE_ID),
            support_resolvers={},
            alert_sink=sink,
        )
    )

    # The hint capture reaches the failing observation; the settled capture is the idle pane.
    with _pane_text(monitor, [_BASE, _BASE + _FORMS["codex"], _BASE]):
        await _observe_pass(monitor)
        await _observe_pass(monitor)
        await _observe_pass(monitor)
        assert await monitor.check_idle_agents() == 1

    failed = agent_run_manager.get(run.id)
    assert failed is not None
    assert failed.status == "error"
    assert failed.terminal_reason == "provider_quota_exhausted"


@pytest.mark.asyncio
async def test_pane_only_hint_never_fails_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    sink = _Sink()
    monitor, run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source="codex"
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))

    with _pane_text(monitor, [_BASE, _BASE + _FORMS["codex"]]):
        await _observe_pass(monitor)
        await _observe_pass(monitor)
        assert await monitor.check_idle_agents() == 0

    assert [edge.provider for edge in sink.hints()] == ["codex"]
    stored = agent_run_manager.get(run.id)
    assert stored is not None and stored.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "grok", "droid", "agy", "qwen"])
async def test_unrelated_source_exhaustion_never_fails_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    source: str,
) -> None:
    sink = _Sink()
    monitor, run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source="codex"
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))

    # Another source's exhaustion text is not evidence against this codex run.
    with _pane_text(monitor, [_BASE, _BASE + _FORMS[source]]):
        await _observe_pass(monitor)
        await _observe_pass(monitor)
        assert await monitor.check_idle_agents() == 0

    assert sink.hints() == []
    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.terminal_reason is None


@pytest.mark.asyncio
async def test_startup_quota_loses_to_finished_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Startup work already finished",
        validation_criteria="The run completes when its task closed before the quota error.",
    )
    transcript = tmp_path / "startup-finished-task.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=str(uuid.uuid4()),
        transcript_path=transcript,
        session_age_seconds=1,
        task_manager=tasks,
        task_id=task.id,
    )
    assert run.started_at is not None
    temp_db.execute(
        "UPDATE terminals SET attempt_started_at = %s WHERE agent_run_id = %s",
        (run.created_at, run.id),
    )
    tasks.close_task(task.id)
    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=_USAGE_ERROR,
        event_timestamp=run.created_at + (run.started_at - run.created_at) / 2,
    )

    with _pane_text(monitor, _BASE):
        first = await monitor.check_idle_agents()
        second = await monitor.check_idle_agents()

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert (first, second) == (1, 0)
    assert stored.status == "success"
    assert stored.terminal_reason != "provider_quota_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt_ms", "anchor", "fails"),
    [
        pytest.param(0, "midpoint", True, id="between_created_and_started"),
        pytest.param(0, "created", True, id="equal_created"),
        pytest.param(250, "attempt", True, id="equal_attempt_started"),
        pytest.param(250, "before_attempt", False, id="predates_attempt"),
        pytest.param(0, "before_created", False, id="predates_run"),
    ],
)
async def test_startup_quota_uses_prelaunch_identity_bounds(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    attempt_ms: int,
    anchor: str,
    fails: bool,
) -> None:
    transcript = tmp_path / f"startup-{anchor}.jsonl"
    monitor, run = _spawned(
        temp_db,
        session_manager,
        sample_project,
        agent_run_manager,
        transcript=transcript,
        session_age_seconds=1,
    )
    created = run.created_at
    assert run.started_at is not None
    attempt_at = created + timedelta(milliseconds=attempt_ms)
    temp_db.execute(
        "UPDATE terminals SET attempt_started_at = %s WHERE agent_run_id = %s",
        (attempt_at, run.id),
    )
    if anchor == "midpoint":
        event_at = created + (run.started_at - created) / 2
    elif anchor == "created":
        event_at = created
    elif anchor == "attempt":
        event_at = attempt_at
    elif anchor == "before_attempt":
        event_at = created + (attempt_at - created) / 2
    else:
        event_at = created - timedelta(seconds=1)
    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=_USAGE_ERROR,
        event_timestamp=event_at,
    )

    with _pane_text(monitor, _BASE):
        first = await monitor.check_idle_agents()
        second = await monitor.check_idle_agents()

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    if fails:
        assert (first, second) == (1, 0)
        assert stored.status == "error"
        assert stored.terminal_reason == "provider_quota_exhausted"
    else:
        assert (first, second) == (0, 0)
        assert stored.status == "running"
        assert stored.terminal_reason is None


@pytest.mark.asyncio
async def test_stale_verified_quota_never_fails_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    transcript = tmp_path / "stale-quota.jsonl"
    monitor, run = _spawned(
        temp_db,
        session_manager,
        sample_project,
        agent_run_manager,
        transcript=transcript,
        session_age_seconds=120,
    )
    # The run and its attempt both predate the error, so only freshness can reject it.
    old = (now - timedelta(seconds=1000)).isoformat()
    temp_db.execute("UPDATE agent_runs SET created_at = %s WHERE id = %s", (old, run.id))
    temp_db.execute(
        "UPDATE terminals SET attempt_started_at = %s WHERE agent_run_id = %s",
        (old, run.id),
    )
    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=_USAGE_ERROR,
        event_timestamp=now - timedelta(seconds=901),
    )

    with _pane_text(monitor, "❯\n"):
        # Completed-turn recovery may still act on an old turn; quota must not fail it.
        await monitor.check_idle_agents()

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert (stored.status, stored.terminal_reason) == ("running", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", ["raised_capture", "unresolved_snapshot"])
async def test_outage_pass_rebaselines_without_emission(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    monkeypatch: pytest.MonkeyPatch,
    outage: str,
) -> None:
    sink = _Sink()
    monitor, _run = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source="codex"
    )
    monitor.set_provider_capacity_service(_service(temp_db, sink))
    unresolved = {"active": False}
    capture = TerminalServices.snapshot

    async def guarded_snapshot(self: Any, *args: Any, **kwargs: Any) -> Any:
        if unresolved["active"]:
            return None
        return await capture(self, *args, **kwargs)

    monkeypatch.setattr(TerminalServices, "snapshot", guarded_snapshot)
    form = _FORMS["codex"]
    texts: list[str | BaseException] = [_BASE]
    if outage == "raised_capture":
        texts.append(RuntimeError("pane gone"))
    texts.extend([_BASE + form, _BASE + form])

    with _pane_text(monitor, texts):
        await _observe_pass(monitor)
        unresolved["active"] = outage == "unresolved_snapshot"
        await _observe_pass(monitor)
        unresolved["active"] = False
        # The form returns after the outage. Its baseline was dropped, so it is a new
        # block against no prior capture; the redraw that follows stays silent.
        await _observe_pass(monitor)
        await _observe_pass(monitor)

    assert sink.hints() == []


def _agy_usage_stdout(*, remaining: float, reset_time: str) -> str:
    """An AGY `/usage` JSON envelope with one bucket, shaped as the reporter parses it."""
    bucket = {"name": "five_hour", "remaining_fraction": remaining, "reset_time": reset_time}
    envelope = {
        "status": "SUCCESS",
        "num_turns": 0,
        "command": {
            "name": "usage",
            "data": {"groups": [{"name": "Models", "buckets": [bucket]}]},
        },
    }
    return json.dumps(envelope)


@pytest.mark.asyncio
async def test_agy_reporter_refresh_reaches_shared_sink(temp_db: HubDatabase) -> None:
    sink = _Sink()

    async def run_command(_command: tuple[str, ...]) -> tuple[int, str, str]:
        return (0, _agy_usage_stdout(remaining=0.0, reset_time=_RESET), "")

    service = _reporter_service(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID),
        sink,
        AgyUsageReporter(run_command=run_command, clock=lambda: _NOW),
    )

    await service.refresh("agy")
    await _settle()

    assert [edge.provider for edge in sink.edges] == ["agy"]


@pytest.mark.asyncio
async def test_agy_reporter_timeout_raises_transient_and_invents_no_edges(
    temp_db: HubDatabase,
) -> None:
    sink = _Sink()
    never = asyncio.Event()

    async def run_command(_command: tuple[str, ...]) -> tuple[int, str, str]:
        await never.wait()
        return (0, "", "")

    reporter = AgyUsageReporter(run_command=run_command, timeout_seconds=0.001)
    with pytest.raises(TransientUsageRefreshError):
        await reporter.report()
    service = _reporter_service(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID), sink, reporter
    )

    await service.refresh("agy")
    await _settle()

    assert sink.edges == []
