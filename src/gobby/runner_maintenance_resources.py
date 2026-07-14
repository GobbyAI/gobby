"""Resource telemetry loop: bounded log growth and daemon self-observation (#18196).

psutil ``io_counters()`` is not implemented on macOS, so disk churn is observed
by sampling per-file sizes in the logs directory each interval — that sampling
is the design, not a fallback. The loop also truncates the OS-level stderr
capture file (``log_file_stderr``) when it exceeds its cap; every writer holds
it in append mode, so truncation to zero is safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def truncate_stderr_log_if_over_cap(stderr_log: Path, max_bytes: int) -> bool:
    """Truncate the stderr capture file when it exceeds ``max_bytes``.

    All writers (Popen stderr, launchd StandardErrorPath, systemd append:)
    hold the file in append mode, so truncating to zero cannot corrupt or
    interleave subsequent writes.
    """
    try:
        if stderr_log.stat().st_size <= max_bytes:
            return False
        os.truncate(stderr_log, 0)
    except OSError:
        return False
    logger.warning("Truncated %s: exceeded %d MB stderr capture cap", stderr_log, max_bytes // _MB)
    return True


def run_resource_check(
    logs_dir: Path,
    stderr_log: Path,
    previous_sizes: dict[str, int] | None,
    *,
    growth_warn_bytes: int,
    stderr_max_bytes: int,
) -> dict[str, int]:
    """One monitor tick: sample sizes, warn on growth, enforce the stderr cap.

    Returns the fresh size sample to carry into the next tick. The first tick
    (``previous_sizes is None``) only records the baseline and enforces the
    stderr cap, which doubles as the truncate-at-daemon-start pass.
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
    if truncate_stderr_log_if_over_cap(stderr_log, stderr_max_bytes):
        sizes[stderr_log.name] = 0
    return sizes


async def resource_monitor_loop(
    telemetry_config: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: float = DEFAULT_RESOURCE_MONITOR_INTERVAL_SECONDS,
) -> None:
    """Background bounded-resource monitor (#18196).

    Watches the logs directory for runaway growth (the incident dirtied 137GB
    of file-backed memory in ~8h) and keeps the fd-level stderr capture file
    under its cap, since no rotating handler owns it.
    """
    log_file = Path(
        str(getattr(telemetry_config, "log_file", "~/.gobby/logs/gobby.log"))
    ).expanduser()
    stderr_log = Path(
        str(getattr(telemetry_config, "log_file_stderr", "~/.gobby/logs/gobby-stderr.log"))
    ).expanduser()
    growth_warn_mb = int(
        getattr(
            telemetry_config,
            "logs_growth_warn_mb_per_interval",
            DEFAULT_LOGS_GROWTH_WARN_MB_PER_INTERVAL,
        )
    )
    stderr_max_mb = int(getattr(telemetry_config, "stderr_log_max_mb", DEFAULT_STDERR_LOG_MAX_MB))
    logs_dir = log_file.parent
    previous: dict[str, int] | None = None

    while not is_shutdown_requested():
        try:
            previous = run_resource_check(
                logs_dir,
                stderr_log,
                previous,
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
