"""Resource telemetry loop: bounded log growth and daemon self-observation (#18196).

psutil ``io_counters()`` is not implemented on macOS, so disk churn is observed
by sampling per-file sizes in the logs directory each interval — that sampling
is the design, not a fallback. The loop reports when the OS-level stderr capture
file exceeds its configured cap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from gobby.config.logging import (
    STDERR_LOG_FILENAME,
    LoggingSettings,
    resolved_log_path,
    resolved_logs_dir,
)

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_MONITOR_INTERVAL_SECONDS = 300.0
DEFAULT_LOGS_GROWTH_WARN_MB_PER_INTERVAL = 100
DEFAULT_STDERR_LOG_MAX_MB = 50

_MB = 1024 * 1024


def _sample_log_sizes(logs_dir: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    try:
        entries = list(logs_dir.iterdir())
    except OSError:
        return sizes
    for entry in entries:
        try:
            if entry.is_file():
                sizes[entry.name] = entry.stat().st_size
        except OSError:
            continue
    return sizes


def _daemon_rss_and_fds() -> tuple[int | None, int | None]:
    try:
        import psutil

        proc = psutil.Process()
        rss = int(proc.memory_info().rss)
        try:
            fds: int | None = int(proc.num_fds())
        except (AttributeError, psutil.Error):
            fds = None
        return rss, fds
    except Exception:
        return None, None


def run_resource_check(
    logs_dir: Path,
    stderr_log: Path,
    previous_sizes: dict[str, int] | None,
    *,
    set_stderr_capture_over_limit: Callable[[bool], None],
    growth_warn_bytes: int,
    stderr_max_bytes: int,
) -> dict[str, int]:
    """One monitor tick: sample sizes and report unhealthy resource growth.

    Returns the fresh size sample to carry into the next tick. The first tick
    (``previous_sizes is None``) only records the baseline and checks the
    stderr capture size.
    """
    sizes = _sample_log_sizes(logs_dir)
    if previous_sizes is not None:
        deltas = {
            name: size - previous_sizes.get(name, 0)
            for name, size in sizes.items()
            if size > previous_sizes.get(name, 0)
        }
        growth = sum(deltas.values())
        if growth >= growth_warn_bytes:
            top = sorted(deltas.items(), key=lambda item: item[1], reverse=True)[:10]
            breakdown = ", ".join(f"{name} +{delta / _MB:.1f}MB" for name, delta in top)
            rss, fds = _daemon_rss_and_fds()
            logger.warning(
                "Logs directory %s grew %.1fMB this interval (cap %dMB): %s "
                "[daemon rss=%sMB fds=%s]",
                logs_dir,
                growth / _MB,
                growth_warn_bytes // _MB,
                breakdown,
                f"{rss / _MB:.0f}" if rss is not None else "?",
                fds if fds is not None else "?",
            )
    try:
        stderr_size = stderr_log.stat().st_size
    except FileNotFoundError:
        set_stderr_capture_over_limit(False)
    except OSError:
        pass
    else:
        over_limit = stderr_size > stderr_max_bytes
        set_stderr_capture_over_limit(over_limit)
        if over_limit:
            logger.warning(
                "Stderr capture %s is %.1fMB, exceeding the configured %dMB limit",
                stderr_log,
                stderr_size / _MB,
                stderr_max_bytes // _MB,
            )
    return sizes


async def resource_monitor_loop(
    logging_config: LoggingSettings,
    is_shutdown_requested: Callable[[], bool],
    set_stderr_capture_over_limit: Callable[[bool], None],
    interval_seconds: float = DEFAULT_RESOURCE_MONITOR_INTERVAL_SECONDS,
) -> None:
    """Background resource monitor (#18196).

    Watches the logs directory for runaway growth (the incident dirtied 137GB
    of file-backed memory in ~8h) and reports when the fd-level stderr capture
    file exceeds its configured cap.
    """
    stderr_log = resolved_log_path(logging_config, STDERR_LOG_FILENAME)
    growth_warn_mb = logging_config.growth_warn_mb_per_interval
    stderr_max_mb = logging_config.runtime_max_size_mb
    logs_dir = resolved_logs_dir(logging_config)
    previous: dict[str, int] | None = None

    while not is_shutdown_requested():
        try:
            previous = run_resource_check(
                logs_dir,
                stderr_log,
                previous,
                set_stderr_capture_over_limit=set_stderr_capture_over_limit,
                growth_warn_bytes=growth_warn_mb * _MB,
                stderr_max_bytes=stderr_max_mb * _MB,
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in resource monitor loop: {e}")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break
