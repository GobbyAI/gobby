"""Tests for the #18196 bounded-resource monitor loop."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_maintenance_resources import (
    run_resource_check,
    truncate_stderr_log_if_over_cap,
)

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
) -> dict[str, int]:
    return run_resource_check(
        logs_dir,
        logs_dir / "gobby-stderr.log",
        previous,
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


def test_stderr_log_truncated_over_cap(logs_dir: Path) -> None:
    stderr_log = logs_dir / "gobby-stderr.log"
    stderr_log.write_bytes(b"x" * (2 * _MB))

    sizes = _check(logs_dir, None, stderr_max_mb=1)

    assert stderr_log.stat().st_size == 0
    assert sizes["gobby-stderr.log"] == 0


def test_stderr_log_under_cap_untouched(logs_dir: Path) -> None:
    stderr_log = logs_dir / "gobby-stderr.log"
    stderr_log.write_bytes(b"x" * 1024)

    assert truncate_stderr_log_if_over_cap(stderr_log, _MB) is False
    assert stderr_log.stat().st_size == 1024


def test_missing_stderr_log_is_tolerated(logs_dir: Path) -> None:
    assert truncate_stderr_log_if_over_cap(logs_dir / "gobby-stderr.log", _MB) is False


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
