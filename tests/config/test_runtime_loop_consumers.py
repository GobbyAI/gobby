from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

import gobby.runner_lifecycle_subsystems as lifecycle_subsystems
from gobby.config.app import DaemonConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.runner import GobbyRunner
from gobby.runner_maintenance_audit import workflow_audit_cleanup_loop
from gobby.runner_service_readiness import require_managed_services_ready
from gobby.scheduler.scheduler import CronScheduler
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.system_automation import SystemAutomationLoop

_LIFECYCLE_MODULES = (
    "src/gobby/runner_init/orchestration.py",
    "src/gobby/runner_lifecycle.py",
    "src/gobby/runner_lifecycle_agents.py",
    "src/gobby/runner_lifecycle_periodic.py",
    "src/gobby/runner_lifecycle_shutdown.py",
    "src/gobby/runner_lifecycle_subsystems.py",
    "src/gobby/runner_service_readiness.py",
)


def _snapshot(
    *,
    active: DaemonConfig,
    desired: DaemonConfig | None = None,
    revision: int,
    pending_restart_keys: frozenset[str] = frozenset(),
) -> ConfigSnapshot:
    return ConfigSnapshot(
        revision=revision,
        desired=desired or active,
        active=active,
        row_revisions={},
        pending_restart_keys=pending_restart_keys,
        failed_live_keys={},
    )


def _bundle(snapshot: ConfigSnapshot) -> RuntimeActiveBundle:
    return RuntimeActiveBundle(snapshot=snapshot, services=MappingProxyType({}))


class BundleRuntime:
    def __init__(self, bundles: list[RuntimeActiveBundle]) -> None:
        self.bundles = bundles
        self.capture_count = 0

    def capture(self) -> RuntimeActiveBundle:
        bundle = self.bundles[min(self.capture_count, len(self.bundles) - 1)]
        self.capture_count += 1
        return bundle


@pytest.mark.asyncio
async def test_periodic_iteration_uses_one_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = BundleRuntime(
        [
            _bundle(
                _snapshot(
                    active=DaemonConfig(session_lifecycle={"workflow_audit_retention_days": 7}),
                    revision=1,
                )
            ),
            _bundle(
                _snapshot(
                    active=DaemonConfig(session_lifecycle={"workflow_audit_retention_days": 30}),
                    revision=2,
                )
            ),
        ]
    )
    retention_days: list[int] = []
    shutdown = False

    class AuditManager:
        def __init__(self, _db: object) -> None:
            pass

        def cleanup_old_entries(self, *, days: int) -> int:
            retention_days.append(days)
            return 0

    async def sleep(_seconds: float) -> None:
        nonlocal shutdown
        if len(retention_days) == 2:
            shutdown = True

    monkeypatch.setattr("gobby.runner_maintenance_audit.WorkflowAuditManager", AuditManager)

    await workflow_audit_cleanup_loop(
        cast(HubDatabase, object()),
        lambda: shutdown,
        capture_bundle=runtime.capture,
        interval_seconds=0,
        sleep=sleep,
    )

    assert retention_days == [7, 30]
    assert runtime.capture_count == 2


@pytest.mark.asyncio
async def test_lifecycle_consumer_observes_live_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BundleRuntime(
        [
            _bundle(
                _snapshot(
                    active=DaemonConfig(code_index={"enabled": False}),
                    revision=1,
                )
            ),
            _bundle(
                _snapshot(
                    active=DaemonConfig(
                        database_url="postgresql://runtime/revision-2",
                        code_index={
                            "enabled": True,
                            "maintenance_index_timeout_seconds": 41,
                        },
                    ),
                    revision=2,
                )
            ),
        ]
    )
    repairs: list[tuple[str, int]] = []

    def repair(database_url: str, *, timeout_seconds: int) -> Mapping[str, Any]:
        repairs.append((database_url, timeout_seconds))
        return {"healthy": True, "indexes": []}

    monkeypatch.setattr("gobby.code_index.bm25_health.repair_bm25_indexes", repair)
    runner = SimpleNamespace(
        config_runtime=runtime,
        startup_config=SimpleNamespace(database_url="postgresql://bootstrap/hub"),
    )

    assert (
        await lifecycle_subsystems._repair_code_index_bm25(cast(GobbyRunner, runner), None) is True
    )
    assert (
        await lifecycle_subsystems._repair_code_index_bm25(cast(GobbyRunner, runner), None) is True
    )

    assert repairs == [("postgresql://bootstrap/hub", 41)]
    assert runtime.capture_count == 2


@pytest.mark.asyncio
async def test_lifecycle_consumer_retains_restart_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = DaemonConfig(
        test_mode=False,
        databases={
            "qdrant": {"url": "http://active-qdrant:6333"},
            "falkordb": {"password": "active-password"},
        },
    )
    desired = DaemonConfig(
        test_mode=True,
        databases={
            "qdrant": {"url": "http://desired-qdrant:6333"},
            "falkordb": {"password": "desired-password"},
        },
    )
    runtime = BundleRuntime(
        [
            _bundle(
                _snapshot(
                    active=active,
                    desired=desired,
                    revision=2,
                    pending_restart_keys=frozenset(
                        {"test_mode", "databases.qdrant.url", "databases.falkordb.password"}
                    ),
                )
            )
        ]
    )
    checked_values: list[tuple[str, str]] = []

    async def check_once(
        _runner: object,
        *,
        qdrant_url: str,
        falkor_config: Any,
    ) -> None:
        checked_values.append((qdrant_url, falkor_config.password))

    monkeypatch.setattr(
        "gobby.runner_service_readiness._check_managed_services_ready_once",
        check_once,
    )
    runner = SimpleNamespace(config_runtime=runtime)

    await require_managed_services_ready(cast(GobbyRunner, runner))

    assert checked_values == [("http://active-qdrant:6333", "active-password")]
    assert runtime.capture_count == 1


@pytest.mark.asyncio
async def test_cron_iteration_uses_one_runtime_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BundleRuntime(
        [
            _bundle(
                _snapshot(
                    active=DaemonConfig(cron={"check_interval_seconds": 61}),
                    revision=1,
                )
            ),
            _bundle(
                _snapshot(
                    active=DaemonConfig(cron={"check_interval_seconds": 122}),
                    revision=2,
                )
            ),
        ]
    )
    scheduler = cast(Any, object.__new__(CronScheduler))
    scheduler._capture_bundle = runtime.capture
    scheduler._running = True
    observed_intervals: list[int] = []
    slept: list[float] = []

    async def check_due_jobs(config: Any) -> None:
        observed_intervals.append(config.check_interval_seconds)
        scheduler._running = False

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    scheduler._check_due_jobs = check_due_jobs
    monkeypatch.setattr("gobby.scheduler.scheduler.asyncio.sleep", sleep)

    await scheduler._check_loop()

    assert observed_intervals == [61]
    assert slept == [61]
    assert runtime.capture_count == 1


def test_system_automation_observes_live_runtime_change() -> None:
    runtime = BundleRuntime(
        [
            _bundle(
                _snapshot(
                    active=DaemonConfig(system_loops={"automation": {"interval_seconds": 11}}),
                    revision=1,
                )
            ),
            _bundle(
                _snapshot(
                    active=DaemonConfig(system_loops={"automation": {"interval_seconds": 22}}),
                    revision=2,
                )
            ),
        ]
    )
    loop = cast(Any, object.__new__(SystemAutomationLoop))
    loop._capture_bundle = runtime.capture

    assert loop.resolve_settings().interval_seconds == 11
    assert loop.resolve_settings().interval_seconds == 22
    assert runtime.capture_count == 2


@pytest.mark.asyncio
async def test_session_process_loop_uses_one_runtime_bundle_per_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BundleRuntime(
        [
            _bundle(
                _snapshot(
                    active=DaemonConfig(
                        session_lifecycle={
                            "transcript_processing_batch_size": 11,
                            "transcript_processing_interval_minutes": 2,
                        }
                    ),
                    revision=1,
                )
            ),
            _bundle(
                _snapshot(
                    active=DaemonConfig(
                        session_lifecycle={
                            "transcript_processing_batch_size": 22,
                            "transcript_processing_interval_minutes": 3,
                        }
                    ),
                    revision=2,
                )
            ),
        ]
    )
    manager = cast(Any, object.__new__(SessionLifecycleManager))
    manager._capture_bundle = runtime.capture
    manager._running = True
    observed_batch_sizes: list[int] = []
    slept: list[float] = []

    async def process_pending(active: DaemonConfig) -> int:
        observed_batch_sizes.append(active.session_lifecycle.transcript_processing_batch_size)
        return 0

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            manager._running = False

    manager._process_pending_transcripts = process_pending
    monkeypatch.setattr("gobby.sessions.lifecycle.asyncio.sleep", sleep)

    await manager._process_loop()

    assert observed_batch_sizes == [11, 22]
    assert slept == [120, 180]
    assert runtime.capture_count == 2


def test_lifecycle_modules_use_runtime_access() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for relative_path in _LIFECYCLE_MODULES:
        path = repository_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "config"
                and isinstance(node.value, ast.Name)
                and node.value.id == "runner"
            ):
                violations.append(f"{relative_path}:{node.lineno}")

    assert violations == []
