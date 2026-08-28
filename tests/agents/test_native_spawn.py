"""Plan 4.2: opt-in native agent launches with SRT, observers, and attention."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from gobby.agents.spawn_executor import execute_spawn, wrap_provider_command
from gobby.agents.spawn_executor_providers import ProviderSpawnPlan
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.config.terminals import TerminalConfig
from gobby.storage.agents import AgentRun
from gobby.storage.terminals import AttachLocator
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.host_protocol import HostListRow
from gobby.terminals.host_reconcile import reconcile_host_inventory
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import (
    Delivered,
    PreparedSpawn,
    ProcessIdentity,
    TerminalSpawnRequest,
)
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.prepared_spawn import prepared_spawn
from tests.agents.test_capture import FakeCaptureStorage
from tests.agents.test_capture import _run as capture_run
from tests.terminals.fakes import MemoryTerminalStore
from tests.terminals.test_native_runtime import FakeHostClient

pytestmark = pytest.mark.unit


@dataclass
class RecordingFrameClient:
    """Records AttachTerminal reservation identity for observer-bind tests."""

    attaches: list[str | None] = field(default_factory=list)
    fail_attach: bool = False

    async def attach_terminal(
        self,
        locator: AttachLocator,
        *,
        reservation_id: str | None = None,
    ) -> None:
        del locator
        if self.fail_attach:
            raise HostCommandError("attach_failed")
        self.attaches.append(reservation_id)


@dataclass
class ObservingHost(FakeHostClient):
    """FakeHostClient plus reserve/release, capacity, and bind tracking."""

    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    entitlements: int = 0
    user_attachments: int = 0
    max_attachments_total: int = 12
    released: list[tuple[str, str]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribed: bool = False
    drop_control_after_prepared: bool = False
    exit_on_commit: bool = False
    first_bytes: bytes = b""
    prepare_calls: int = 0

    def entitlement_ceiling(self) -> int:
        return self.max_attachments_total - 4

    async def reserve_observer(self, terminal_id: str, reserve_key: str) -> dict[str, Any]:
        await self.ensure_connected()
        for existing in self.reservations.values():
            if existing["terminal_id"] == terminal_id and existing["reserve_key"] == reserve_key:
                return {
                    "ok": True,
                    "reservation_id": existing["reservation_id"],
                    "reserve_key": reserve_key,
                    "reserve_generation": existing["generation"],
                }
        if self.entitlements >= self.entitlement_ceiling():
            raise HostCommandError("capacity")
        reservation_id = f"rsv-{uuid4()}"
        self.reservations[reservation_id] = {
            "reservation_id": reservation_id,
            "reserve_key": reserve_key,
            "terminal_id": terminal_id,
            "generation": 1,
            "prepared": False,
        }
        self.entitlements += 1
        return {
            "ok": True,
            "reservation_id": reservation_id,
            "reserve_key": reserve_key,
            "reserve_generation": 1,
        }

    async def release_observer(self, reservation_id: str, reserve_key: str) -> dict[str, Any]:
        self.released.append((reservation_id, reserve_key))
        current = self.reservations.get(reservation_id)
        if current is None:
            return {"ok": True, "released": True}
        if current["prepared"]:
            return {"ok": True, "released": False}
        if current["reserve_key"] != reserve_key:
            return {"ok": True, "released": False}
        del self.reservations[reservation_id]
        self.entitlements = max(0, self.entitlements - 1)
        return {"ok": True, "released": True}

    async def spawn(self, **fields: Any) -> dict[str, Any]:
        self.prepare_calls += 1
        payload = await super().spawn(**fields)
        reservation_id = str(fields.get("reservation_id") or "")
        reserved = self.reservations.get(reservation_id)
        if reserved is not None:
            reserved["prepared"] = True
        self.observer_bind = "reserved"
        if self.drop_control_after_prepared:
            self.available = False
            raise ConnectionError("control dropped after spawn_prepared")
        if self.first_bytes:
            self.pty.append(self.first_bytes)
        return payload

    async def spawn_commit(self, terminal_id: str, spawn_key: str) -> None:
        await super().spawn_commit(terminal_id, spawn_key)
        if self.exit_on_commit:
            self.children_alive = False
            self.events.append(
                {"method": "terminal_exited", "terminal_id": terminal_id, "spawn_key": spawn_key}
            )

    async def subscribe_events(self) -> dict[str, Any]:
        self.subscribed = True
        return {"ok": True, "subscribed": True}

    def fill_user_attachments(self, count: int) -> None:
        self.user_attachments = count


def _list_row(
    *,
    terminal_id: str,
    spawn_key: str,
    observer_bind: Literal["reserved", "bound", "entitled", "none"],
    host_terminal_id: str = "ht-1",
) -> HostListRow:
    return HostListRow(
        terminal_id=terminal_id,
        spawn_key=spawn_key,
        commit_state="prepared",
        observer_bind=observer_bind,
        host_terminal_id=host_terminal_id,
    )


def _srt_launch() -> SandboxLaunch:
    return SandboxLaunch(
        backend="srt",
        enforced=True,
        runtime_version="0.0.66",
        policy_hash="hash",
        policy_path="/policy/settings.json",
        violation_path="/policy/violations.jsonl",
        provider_executable="/opt/claude/versions/2.1.220",
        node_path="/managed/node",
        runner_path="/managed/runner.mjs",
    )


def _plan(*, command: list[str] | None = None) -> ProviderSpawnPlan:
    return ProviderSpawnPlan(
        command=command or ["/usr/local/bin/claude", "--session-id", "child"],
        env={"GOBBY_SESSION_ID": "child"},
        launch=_srt_launch(),
        auth_cli="claude",
        child_session_id="child",
        agent_run_id="run-native",
        title="claude",
    )


def _native_request(
    *,
    host: ObservingHost,
    frame: RecordingFrameClient,
    manager: MemoryTerminalStore | None = None,
    **overrides: Any,
) -> tuple[SpawnRequest, NativeTerminalRuntime, MemoryTerminalStore]:
    store = manager or MemoryTerminalStore()
    runtime = NativeTerminalRuntime(
        host,
        frame_host_epoch=host.host_epoch,
        terminal_manager=store,
        machine_id="machine-1",
        frame_client=frame,
    )
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    values: dict[str, Any] = {
        "prompt": "go",
        "cwd": "/workspace",
        "provider": "claude",
        "session_id": "parent",
        "run_id": "run-native",
        "parent_session_id": "parent",
        "project_id": "project",
        "machine_id": "machine-1",
        "prepared_spawn": prepared_spawn(),
        "terminal_backend": "native",
        "terminal_manager": store,
        "terminal_runtime_registry": registry,
        "daemon_config": MagicMock(terminals=TerminalConfig()),
    }
    values.update(overrides)
    return SpawnRequest(**values), runtime, store


@pytest.mark.asyncio
async def test_srt_wrapped_native_launch() -> None:
    host = ObservingHost()
    frame = RecordingFrameClient()
    request, _runtime, manager = _native_request(host=host, frame=frame)
    plan = _plan()
    wrapped = wrap_provider_command(plan.launch, plan.command)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=plan),
    ):
        result = await execute_spawn(request)
    assert result.success is True
    assert result.terminal_type == "native"
    row = manager.get(result.terminal_id or "")
    assert row is not None
    assert row.backend == "native"
    assert host.spawns, "control spawn must run"
    argv = host.spawns[0]["argv"]
    assert argv == wrapped
    assert argv[0] == "/managed/node"
    assert "--settings" in argv
    assert plan.launch.provider_executable in argv
    assert host.spawns[0]["reservation_id"]
    assert frame.attaches == [host.spawns[0]["reservation_id"]]


@pytest.mark.asyncio
async def test_attention_episode_native(temp_db: Any) -> None:
    from gobby.agents.attention_tracker import AgentAttentionTracker
    from gobby.agents.prompt_detector import PromptDetector
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.storage.attention import AttentionStateManager
    from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest

    host = ObservingHost()
    host.snapshot_text = "Permission required: press Enter to approve this command"
    frame = RecordingFrameClient()
    request, runtime, manager = _native_request(host=host, frame=frame)
    plan = _plan()
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=plan),
    ):
        spawned = await execute_spawn(request)
    assert spawned.success is True
    terminal = manager.get(spawned.terminal_id or "")
    assert terminal is not None
    snapshot = await runtime.snapshot(terminal)
    attention_manager = AttentionStateManager(temp_db, epoch="native-attention")

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    now = datetime.now(UTC)
    tracker = AgentAttentionTracker(
        run_db=run_db,
        prompt_detector=PromptDetector(BundledDetectionRegistry(), "claude"),
        stall_classifier=StallClassifier(BundledDetectionRegistry(), "claude"),
        tmux_config=MagicMock(auto_enter_approval_prompts=False),
        attention_manager=attention_manager,
    )
    run = AgentRun(
        id="run-native",
        parent_session_id="parent",
        provider="claude",
        prompt="go",
        status="running",
        created_at=now,
        updated_at=now,
        child_session_id="child",
        terminal_id=terminal.id,
    )
    await tracker.sync(run, snapshot.text)
    episode = attention_manager.get("run:run-native")
    assert episode is not None
    assert episode.state == "blocked"
    coordinator = WriteCoordinator(cast(Any, manager), runtime)
    outcome = await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key=f"attention:{episode.attention_id}",
            origin="attention",
            kind="key",
            payload="enter",
        )
    )
    assert isinstance(outcome, Delivered)
    assert host.pty[-1] == b"\n"
    await tracker.clear_after_injection(run)
    cleared = attention_manager.get("run:run-native")
    assert cleared is None or cleared.state is None


@dataclass
class SimpleRun:
    status: str
    terminal_id: str
    id: str = "run-native"


@pytest.mark.asyncio
async def test_streaming_wait_capture_exit() -> None:
    from gobby.mcp_proxy.tools.agents import create_agents_registry

    outputs: list[str] = []
    host = ObservingHost()
    host.first_bytes = b"READY: port 60887\n"
    host.snapshot_text = "READY: port 60887"
    host.exit_on_commit = True
    frame = RecordingFrameClient()
    last_request: SpawnRequest | None = None
    streams: list[str] = []
    for index in range(5):
        host.fill_user_attachments(8)
        request, _runtime, _manager = _native_request(
            host=host,
            frame=frame,
            run_id=f"run-{index}",
        )
        last_request = request
        plan = _plan(command=["/bin/echo", f"stream-{index}"])
        with patch(
            "gobby.agents.spawn_executor.prepare_claude_spawn",
            new=AsyncMock(return_value=plan),
        ):
            result = await execute_spawn(request)
        assert result.success is True
        streams.append(result.terminal_id or "")
        outputs.append(host.first_bytes.decode("utf-8"))
    assert len(streams) == 5
    assert outputs == ["READY: port 60887\n"] * 5
    assert last_request is not None

    runner = MagicMock()
    runner.get_run.return_value = SimpleRun(status="running", terminal_id=streams[-1])
    runner.database = None
    runner.db = None
    runner.terminal_runtime_registry = last_request.terminal_runtime_registry
    runner.terminal_manager = last_request.terminal_manager
    registry = create_agents_registry(runner)
    wait_for_output = registry._tools["wait_for_output"].func
    matched = await wait_for_output(
        "run-native",
        pattern=r"READY: port \d+",
        timeout_seconds=1,
        poll_interval_seconds=0.05,
    )
    assert matched["success"] is True
    assert matched["matched"] is True


@pytest.mark.asyncio
async def test_oversized_unicode_capture_metadata_reaches_storage() -> None:
    from gobby.agents.capture import parse_capture_slot, terminate_managed_runtime_async

    host = ObservingHost()
    wide = "中" * 100
    encoded = wide.encode("utf-8")
    cut = encoded[:10]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    if cut and (cut[-1] & 0xC0) == 0xC0:
        cut = cut[:-1]
    host.snapshot_text = cut.decode("utf-8")
    host.snapshot_truncated = True
    host.snapshot_dropped = len(encoded) - len(cut)
    host.snapshot_total = len(encoded)
    frame = RecordingFrameClient()
    request, runtime, manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        spawned = await execute_spawn(request)
    terminal = manager.get(spawned.terminal_id or "")
    assert terminal is not None
    storage = FakeCaptureStorage(capture_run("native-cap"))
    result = await terminate_managed_runtime_async(
        storage=storage,
        run=storage.runs["native-cap"],
        terminal=terminal,
        runtime=runtime,
        action="fail",
        reason="killed",
    )
    assert result.success is True
    parsed = parse_capture_slot(storage.runs["native-cap"].result or "")
    assert parsed.truncated is True
    assert parsed.dropped_bytes == host.snapshot_dropped
    assert parsed.total_bytes == host.snapshot_total
    parsed.text.encode("utf-8")

    host.snapshot_truncated = False
    host.snapshot_dropped = 0
    host.snapshot_text = "ok"
    host.snapshot_total = 2
    storage = FakeCaptureStorage(capture_run("native-full"))
    result = await terminate_managed_runtime_async(
        storage=storage,
        run=storage.runs["native-full"],
        terminal=terminal,
        runtime=runtime,
        action="complete",
    )
    assert result.success is True
    parsed = parse_capture_slot(storage.runs["native-full"].result or "")
    assert parsed.truncated is False
    assert parsed.dropped_bytes == 0
    assert parsed.text == "ok"


@pytest.mark.asyncio
async def test_exit_finalization_survives_downtime_and_duplicates() -> None:
    host = ObservingHost()
    host.exit_on_commit = True
    frame = RecordingFrameClient()
    request, _runtime, manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        spawned = await execute_spawn(request)
    assert host.subscribed is True
    terminal_id = spawned.terminal_id or ""
    first = manager.mark_exited(terminal_id)
    duplicate = manager.mark_exited(terminal_id)
    assert first is not None
    assert duplicate is None or duplicate.state == "exited"

    live = MemoryTerminalStore()
    request2, _runtime2, live = _native_request(host=ObservingHost(), frame=RecordingFrameClient())
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        second = await execute_spawn(request2)
    live_row = live.get(second.terminal_id or "")
    assert live_row is not None
    live_row.state = "live"
    live_row.host_epoch = "epoch-1"

    async def kill(_host_id: str) -> None:
        return None

    await reconcile_host_inventory(
        terminal_manager=live,
        machine_id="machine-1",
        host_epoch="epoch-1",
        host_rows=[],
        spawn_in_doubt_seconds=0.0,
        run_manager=None,
        kill=kill,
    )
    finalized = live.get(second.terminal_id or "")
    assert finalized is not None
    assert finalized.state == "exited"
    health_source = inspect.getsource(TerminalHostManager._health_loop)
    assert "reconcile" in health_source


@pytest.mark.asyncio
async def test_observer_bound_before_spawn_commit() -> None:
    host = ObservingHost()
    host.first_bytes = b"hello"
    host.exit_on_commit = True
    frame = RecordingFrameClient()
    request, _runtime, _manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        result = await execute_spawn(request)
    assert result.success is True
    assert host.commits
    assert frame.attaches == [host.spawns[0]["reservation_id"]]
    assert b"".join(host.pty).startswith(b"hello")

    fail_host = ObservingHost()
    fail_frame = RecordingFrameClient(fail_attach=True)
    fail_request, _fail_runtime, fail_manager = _native_request(host=fail_host, frame=fail_frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        failed = await execute_spawn(fail_request)
    assert failed.success is False
    assert fail_host.commits == []
    assert fail_host.kills
    reservation_id = fail_host.spawns[0]["reservation_id"]
    release = await fail_host.release_observer(
        reservation_id, str(fail_host.spawns[0]["reserve_key"])
    )
    assert release["released"] is False
    row = fail_manager.get(failed.terminal_id or "")
    assert row is not None
    assert row.state != "live"


@pytest.mark.asyncio
async def test_prepared_reconnect_rebinds_before_commit() -> None:
    host = ObservingHost()
    frame = RecordingFrameClient()
    request, runtime, _manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        spawned = await execute_spawn(request)
    reservation_id = host.spawns[0]["reservation_id"]
    prepared = PreparedSpawn(
        terminal_id=UUID(spawned.terminal_id or str(uuid4())),
        spawn_key=str(host.spawns[0]["spawn_key"]),
        locator=AttachLocator(
            backend="native",
            frame_host_epoch=host.host_epoch,
            host_terminal_id="ht-1",
        ),
        process=ProcessIdentity(pgid=99, start_time=1),
        host_terminal_id="ht-1",
    )
    host.list_rows = [
        _list_row(
            terminal_id=str(prepared.terminal_id),
            spawn_key=prepared.spawn_key,
            observer_bind="reserved",
        )
    ]
    await runtime.rebind_prepared(prepared, reservation_id=reservation_id)
    assert reservation_id in frame.attaches or reservation_id in host.attaches

    host.list_rows[0] = _list_row(
        terminal_id=str(prepared.terminal_id),
        spawn_key=prepared.spawn_key,
        observer_bind="none",
    )
    with pytest.raises(HostCommandError, match="observer_bind_none"):
        await runtime.rebind_prepared(prepared, reservation_id=reservation_id)


@pytest.mark.asyncio
async def test_prepared_frame_loss_saturates_then_expires() -> None:
    host = ObservingHost(max_attachments_total=8)
    frame = RecordingFrameClient()
    plan = _plan()
    ceiling = host.entitlement_ceiling()
    for _ in range(ceiling):
        request, _runtime, _manager = _native_request(host=host, frame=frame)
        with patch(
            "gobby.agents.spawn_executor.prepare_claude_spawn",
            new=AsyncMock(return_value=plan),
        ):
            result = await execute_spawn(request)
        assert result.success is True
    prepares_before_overflow = host.prepare_calls
    overflow_request, _overflow_runtime, _overflow_manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=plan),
    ):
        refused = await execute_spawn(overflow_request)
    assert refused.success is False
    assert "capacity" in (refused.error or "") or "reserve" in (refused.error or "")
    assert host.prepare_calls == prepares_before_overflow
    host.entitlements = max(0, host.entitlements - 1)
    later, _later_runtime, _later_manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=plan),
    ):
        recovered = await execute_spawn(later)
    assert recovered.success is True


@pytest.mark.asyncio
async def test_committed_observer_rebinds_under_user_saturation() -> None:
    host = ObservingHost()
    host.fill_user_attachments(8)
    frame = RecordingFrameClient()
    request, runtime, _manager = _native_request(host=host, frame=frame)
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        spawned = await execute_spawn(request)
    reservation_id = host.spawns[0]["reservation_id"]
    host.list_rows = [
        _list_row(
            terminal_id=spawned.terminal_id or "",
            spawn_key=str(host.spawns[0]["spawn_key"]),
            observer_bind="entitled",
        )
    ]
    prepared = PreparedSpawn(
        terminal_id=UUID(spawned.terminal_id or str(uuid4())),
        spawn_key=str(host.spawns[0]["spawn_key"]),
        locator=AttachLocator(
            backend="native", frame_host_epoch=host.host_epoch, host_terminal_id="ht-1"
        ),
        process=ProcessIdentity(pgid=99, start_time=1),
        host_terminal_id="ht-1",
    )
    await runtime.rebind_prepared(prepared, reservation_id=reservation_id)
    assert reservation_id in frame.attaches or reservation_id in host.attaches
    assert host.user_attachments == 8


@pytest.mark.asyncio
async def test_spawn_carries_reservation_identity() -> None:
    host = ObservingHost()
    frame = RecordingFrameClient()
    request, runtime, _manager = _native_request(host=host, frame=frame)
    first = await runtime.reserve_observer(UUID("11111111-1111-4111-8111-111111111111"))
    second = await runtime.reserve_observer(UUID("11111111-1111-4111-8111-111111111111"))
    assert first["reservation_id"] == second["reservation_id"]
    distinct = await runtime.reserve_observer(UUID("22222222-2222-4222-8222-222222222222"))
    with patch(
        "gobby.agents.spawn_executor.prepare_claude_spawn",
        new=AsyncMock(return_value=_plan()),
    ):
        spawned = await execute_spawn(request)
    transferred = host.spawns[0]["reservation_id"]
    assert transferred != distinct["reservation_id"]
    stale = TerminalSpawnRequest(
        terminal_id=UUID(spawned.terminal_id or str(uuid4())),
        spawn_key="stale",
        command=["true"],
        reservation_id="rsv-stale",
        reserve_key="other",
    )
    host.reservation_error = "stale_reservation"
    with pytest.raises(HostCommandError):
        await runtime.prepare_spawn(stale)
