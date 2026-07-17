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

from gobby.hooks.runtime_compat import GhookRuntimeDiagnostic, GhookRuntimeState
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_maintenance_resources import resource_monitor_loop, run_resource_check
from gobby.servers.routes.admin import create_admin_router
from gobby.utils.status import format_status_message

pytestmark = pytest.mark.unit

_MB = 1024 * 1024


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    return logs


def _check(
    logs_dir: Path,
    previous: dict[str, int] | None,
    *,
    growth_warn_mb: int = 1,
    stderr_max_mb: int = 1,
    set_stderr_capture_over_limit: Callable[[bool], None] | None = None,
) -> dict[str, int]:
    update_limit_state = set_stderr_capture_over_limit or (lambda _: None)
    return run_resource_check(
        logs_dir,
        logs_dir / "gobby-stderr.log",
        previous,
        set_stderr_capture_over_limit=update_limit_state,
        growth_warn_bytes=growth_warn_mb * _MB,
        stderr_max_bytes=stderr_max_mb * _MB,
    )


def test_first_tick_records_baseline_without_warning(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (logs_dir / "gobby.log").write_bytes(b"x" * (5 * _MB))

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        sizes = _check(logs_dir, None)

    assert sizes == {"gobby.log": 5 * _MB}
    assert not caplog.records


def test_growth_over_cap_warns_with_per_file_attribution(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (logs_dir / "gobby.log").write_bytes(b"x" * (3 * _MB))
    (logs_dir / "recall_signal.jsonl").write_bytes(b"x" * (100 * 1024))
    previous = {"gobby.log": 0, "recall_signal.jsonl": 50 * 1024}

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        _check(logs_dir, previous, growth_warn_mb=1)

    [record] = caplog.records
    message = record.getMessage()
    assert "grew" in message
    assert "gobby.log +3.0MB" in message
    assert "recall_signal.jsonl" in message


def test_steady_state_stays_silent(logs_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    (logs_dir / "gobby.log").write_bytes(b"x" * (3 * _MB))
    previous = _check(logs_dir, None)

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        sizes = _check(logs_dir, previous, growth_warn_mb=1)

    assert not caplog.records
    assert sizes == previous


def test_shrinking_files_do_not_offset_growth(
    logs_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (logs_dir / "grower.log").write_bytes(b"x" * (2 * _MB))
    (logs_dir / "shrinker.log").write_bytes(b"")
    previous = {"grower.log": 0, "shrinker.log": 10 * _MB}

    with caplog.at_level(logging.WARNING, logger="gobby.runner_maintenance_resources"):
        _check(logs_dir, previous, growth_warn_mb=1)

    [record] = caplog.records
    assert "grower.log +2.0MB" in record.getMessage()


def test_stderr_log_over_cap_is_left_intact(logs_dir: Path) -> None:
    stderr_log = logs_dir / "gobby-stderr.log"
    captured = b"x" * (2 * _MB)
    stderr_log.write_bytes(captured)
    limit_states: list[bool] = []

    sizes = _check(
        logs_dir,
        None,
        stderr_max_mb=1,
        set_stderr_capture_over_limit=limit_states.append,
    )

    assert stderr_log.read_bytes() == captured
    assert sizes["gobby-stderr.log"] == len(captured)
    assert limit_states == [True]


def test_stderr_log_under_cap_untouched(logs_dir: Path) -> None:
    stderr_log = logs_dir / "gobby-stderr.log"
    stderr_log.write_bytes(b"x" * 1024)
    limit_states: list[bool] = []

    _check(logs_dir, None, set_stderr_capture_over_limit=limit_states.append)

    assert stderr_log.stat().st_size == 1024
    assert limit_states == [False]


def test_missing_stderr_log_is_tolerated(logs_dir: Path) -> None:
    limit_states: list[bool] = []

    _check(logs_dir, None, set_stderr_capture_over_limit=limit_states.append)

    assert limit_states == [False]


def test_start_periodic_tasks_registers_resource_monitor() -> None:
    from gobby.config.bin_freshness import BinFreshnessConfig

    telemetry = SimpleNamespace(trace_retention_days=7)
    runner: Any = SimpleNamespace(
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        degraded_services=set(),
        _shutdown_requested=False,
        config=SimpleNamespace(
            telemetry=telemetry,
            bin_freshness=BinFreshnessConfig(enabled=False),
            chat=None,
        ),
    )
    monitor_args: list[tuple[Any, ...]] = []

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    def resource_monitor_loop(*args: Any, **kwargs: Any) -> Any:
        monitor_args.append(args)
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
    assert monitor_args[0][0] is telemetry
    set_stderr_capture_over_limit = monitor_args[0][2]
    set_stderr_capture_over_limit(True)
    assert runner.degraded_services == {"stderr_capture_over_limit"}
    set_stderr_capture_over_limit(False)
    assert runner.degraded_services == set()


def test_stderr_limit_degradation_is_visible_in_health_endpoint_and_recovers(
    tmp_path: Path,
) -> None:
    stderr_log = tmp_path / "gobby-stderr.log"
    telemetry = SimpleNamespace(
        log_file=str(tmp_path / "gobby.log"),
        log_file_stderr=str(stderr_log),
        logs_growth_warn_mb_per_interval=100,
        stderr_log_max_mb=1,
    )
    runner = SimpleNamespace(degraded_services=set())
    server = MagicMock()
    server.get_runner.return_value = runner
    app = FastAPI()
    app.include_router(create_admin_router(server))
    client = TestClient(app)

    def set_stderr_capture_over_limit(over_limit: bool) -> None:
        if over_limit:
            runner.degraded_services.add("stderr_capture_over_limit")
        else:
            runner.degraded_services.discard("stderr_capture_over_limit")

    def run_monitor_tick() -> None:
        shutdown_checks = 0

        def is_shutdown_requested() -> bool:
            nonlocal shutdown_checks
            shutdown_checks += 1
            return shutdown_checks > 1

        asyncio.run(
            resource_monitor_loop(
                telemetry,
                is_shutdown_requested,
                set_stderr_capture_over_limit,
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
        stderr_log.write_bytes(b"x" * (2 * _MB))
        run_monitor_tick()
        over_limit_response = client.get("/api/admin/health")

        stderr_log.write_bytes(b"x" * 1024)
        run_monitor_tick()
        recovered_response = client.get("/api/admin/health")

    assert over_limit_response.status_code == 200
    assert over_limit_response.json()["status"] == "degraded"
    assert over_limit_response.json()["degraded_services"] == ["stderr_capture_over_limit"]
    assert recovered_response.status_code == 200
    assert recovered_response.json()["status"] == "ok"
    assert recovered_response.json()["degraded_services"] == []


def test_status_message_displays_stderr_capture_degradation() -> None:
    message = format_status_message(
        running=True,
        api_data={"degraded_services": ["stderr_capture_over_limit"]},
    )

    assert "Health Issues:" in message
    assert "Degraded service: stderr_capture_over_limit" in message
