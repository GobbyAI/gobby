"""Tests for the #18196 bounded-resource monitor loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.logging import LoggingSettings
from gobby.hooks.runtime_compat import GhookRuntimeDiagnostic, GhookRuntimeState
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_maintenance_resources import (
    LogFileSizes,
    resource_monitor_loop,
    run_resource_check,
)
from gobby.servers.routes.admin import create_admin_router, create_health_router
from gobby.utils.status import format_status_message
from tests.config_runtime_helpers import static_runtime_capture

pytestmark = pytest.mark.unit

_MB = 1024 * 1024


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    return logs


def _check(
    logs_dir: Path,
    previous: LogFileSizes | None,
    *,
    growth_warn_mb: int = 1,
    runtime_max_mb: int = 1,
    set_runtime_output_over_limit: Callable[[bool], None] | None = None,
) -> LogFileSizes:
    update_limit_state = set_runtime_output_over_limit or (lambda _: None)
    return run_resource_check(
        logs_dir,
        logs_dir / "runtime.log",
        previous,
        set_runtime_output_over_limit=update_limit_state,
        growth_warn_bytes=growth_warn_mb * _MB,
        runtime_max_bytes=runtime_max_mb * _MB,
    )


def test_first_tick_records_baseline_without_warning(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (logs_dir / "daemon.log").write_bytes(b"x" * (5 * _MB))

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        sizes = _check(logs_dir, None)

    assert list(sizes.values()) == [("daemon.log", 5 * _MB)]
    assert not caplog.records


def test_growth_over_cap_warns_with_per_file_attribution(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (logs_dir / "daemon.log").write_bytes(b"")
    (logs_dir / "recall_signal.jsonl").write_bytes(b"x" * (50 * 1024))
    previous = _check(logs_dir, None)
    (logs_dir / "daemon.log").write_bytes(b"x" * (3 * _MB))
    (logs_dir / "recall_signal.jsonl").write_bytes(b"x" * (100 * 1024))

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        _check(logs_dir, previous, growth_warn_mb=1)

    [record] = caplog.records
    message = record.getMessage()
    assert "grew" in message
    assert "daemon.log +3.0MB" in message
    assert "recall_signal.jsonl" in message


def test_steady_state_stays_silent(logs_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    (logs_dir / "daemon.log").write_bytes(b"x" * (3 * _MB))
    previous = _check(logs_dir, None)

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        sizes = _check(logs_dir, previous, growth_warn_mb=1)

    assert not caplog.records
    assert sizes == previous


def test_shrinking_files_do_not_offset_growth(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (logs_dir / "grower.log").write_bytes(b"")
    (logs_dir / "shrinker.log").write_bytes(b"x" * (10 * _MB))
    previous = _check(logs_dir, None)
    (logs_dir / "grower.log").write_bytes(b"x" * (2 * _MB))
    (logs_dir / "shrinker.log").write_bytes(b"")

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        _check(logs_dir, previous, growth_warn_mb=1)

    [record] = caplog.records
    assert "grower.log +2.0MB" in record.getMessage()


def test_log_rotation_preserves_inode_identity_without_false_growth(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    active_log = logs_dir / "daemon.log"
    active_log.write_bytes(b"x" * (2 * _MB))
    previous = _check(logs_dir, None)
    active_log.rename(logs_dir / "daemon.log.1")
    active_log.write_bytes(b"new")

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        _check(logs_dir, previous, growth_warn_mb=1)

    assert not caplog.records


def test_runtime_log_over_cap_is_left_intact(logs_dir: Path) -> None:
    runtime_log = logs_dir / "runtime.log"
    captured = b"x" * (2 * _MB)
    runtime_log.write_bytes(captured)
    limit_states: list[bool] = []

    sizes = _check(
        logs_dir,
        None,
        runtime_max_mb=1,
        set_runtime_output_over_limit=limit_states.append,
    )

    assert runtime_log.read_bytes() == captured
    assert list(sizes.values()) == [("runtime.log", len(captured))]
    assert limit_states == [True]


def test_runtime_log_under_cap_untouched(logs_dir: Path) -> None:
    runtime_log = logs_dir / "runtime.log"
    runtime_log.write_bytes(b"x" * 1024)
    limit_states: list[bool] = []

    _check(logs_dir, None, set_runtime_output_over_limit=limit_states.append)

    assert runtime_log.stat().st_size == 1024
    assert limit_states == [False]


def test_missing_runtime_log_is_tolerated(logs_dir: Path) -> None:
    limit_states: list[bool] = []

    _check(logs_dir, None, set_runtime_output_over_limit=limit_states.append)

    assert limit_states == [False]


@pytest.mark.parametrize(
    ("integration_enabled", "coordinator_started"),
    [(True, True), (False, False)],
)
def test_start_periodic_tasks_registers_resource_monitor(
    integration_enabled: bool,
    coordinator_started: bool,
) -> None:
    from gobby.config.bin_freshness import BinFreshnessConfig

    logging_config = LoggingSettings(dir="/tmp/gobby-resource-monitor-test")
    mcp_proxy = MagicMock()
    mcp_proxy.get_server_config.side_effect = lambda name: (
        SimpleNamespace(enabled=integration_enabled) if name == "github" else None
    )
    runner: Any = SimpleNamespace(
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        mcp_proxy=mcp_proxy,
        task_manager=object(),
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        degraded_services=set(),
        _shutdown_requested=False,
        config_runtime=SimpleNamespace(
            capture=lambda: SimpleNamespace(
                snapshot=SimpleNamespace(
                    active=DaemonConfig(
                        logging=logging_config,
                        bin_freshness=BinFreshnessConfig(enabled=False),
                    )
                )
            )
        ),
    )
    monitor_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    def resource_monitor_loop(*args: Any, **kwargs: Any) -> Any:
        monitor_calls.append((args, kwargs))
        return noop()

    def fake_create_task(coro: Any, *, name: str | None = None) -> MagicMock:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        task = MagicMock()
        task.name = name
        return task

    with patch("gobby.runner_lifecycle_periodic.asyncio.create_task", side_effect=fake_create_task):
        start_periodic_tasks(runner, tracker=None, resource_monitor_loop=resource_monitor_loop)

    assert runner._resource_monitor_task is not None
    assert (runner._external_issue_sync_task is not None) is coordinator_started
    assert (runner.external_issue_sync_coordinator is not None) is coordinator_started
    monitor_args, monitor_kwargs = monitor_calls[0]
    assert monitor_kwargs["capture_bundle"]().snapshot.active.logging == logging_config
    set_runtime_output_over_limit = monitor_args[1]
    set_runtime_output_over_limit(True)
    assert runner.degraded_services == {"runtime_output_over_limit"}
    set_runtime_output_over_limit(False)
    assert runner.degraded_services == set()


def test_runtime_limit_degradation_is_visible_in_health_endpoint_and_recovers(
    tmp_path: Path,
) -> None:
    runtime_log = tmp_path / "runtime.log"
    logging_config = LoggingSettings(
        dir=str(tmp_path),
        growth_warn_mb_per_interval=100,
        runtime_max_size_mb=1,
    )
    runner = SimpleNamespace(degraded_services=set())
    server = MagicMock()
    server.get_runner.return_value = runner
    app = FastAPI()
    app.include_router(create_admin_router(server))
    app.include_router(create_health_router(server))
    client = TestClient(app)

    def set_runtime_output_over_limit(over_limit: bool) -> None:
        if over_limit:
            runner.degraded_services.add("runtime_output_over_limit")
        else:
            runner.degraded_services.discard("runtime_output_over_limit")

    def run_monitor_tick() -> None:
        shutdown_checks = 0

        def is_shutdown_requested() -> bool:
            nonlocal shutdown_checks
            shutdown_checks += 1
            return shutdown_checks > 1

        asyncio.run(
            resource_monitor_loop(
                is_shutdown_requested,
                set_runtime_output_over_limit,
                capture_bundle=static_runtime_capture(DaemonConfig(logging=logging_config)),
                interval_seconds=0,
            )
        )

    diagnostic = GhookRuntimeDiagnostic(
        state=GhookRuntimeState.COMPATIBLE,
        stamp_path="/tmp/.ghook-runtime.json",
        detail="runtime compatible",
        schema_version=1,
        ghook_version="0.7.1",
    )
    with patch(
        "gobby.servers.routes.admin._health.read_ghook_runtime_diagnostic",
        return_value=diagnostic,
    ):
        runtime_log.write_bytes(b"x" * (2 * _MB))
        run_monitor_tick()
        over_limit_response = client.get("/api/health")

        runtime_log.write_bytes(b"x" * 1024)
        run_monitor_tick()
        recovered_response = client.get("/api/health")

    assert over_limit_response.status_code == 200
    assert over_limit_response.json()["status"] == "degraded"
    assert over_limit_response.json()["degraded_services"] == ["runtime_output_over_limit"]
    assert recovered_response.status_code == 200
    assert recovered_response.json()["status"] == "ok"
    assert recovered_response.json()["degraded_services"] == []


def test_status_message_displays_runtime_output_degradation() -> None:
    message = format_status_message(
        running=True,
        api_data={"degraded_services": ["runtime_output_over_limit"]},
    )

    assert "Health Issues:" in message
    assert "Degraded service: runtime_output_over_limit" in message
