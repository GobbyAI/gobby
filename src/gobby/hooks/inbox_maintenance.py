"""Hook inbox replay cadence and file retention."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from random import SystemRandom
from typing import Any, Final

from gobby.hooks.envelope_dedupe import (
    DirectoryPruneResult,
    get_processed_envelope_dir,
    prune_directory_by_age,
    prune_processed_envelope_markers,
)
from gobby.hooks.inbox import (
    _RECEIPT_CLAIM_SUFFIX,
    drain_hook_inbox_once,
)
from gobby.hooks.inbox_envelopes import get_hook_inbox_dir

logger = logging.getLogger(__name__)
_JITTER_RANDOM = SystemRandom()

# How long an abandoned temp file must sit before the reaper takes it. ghook
# writes an envelope as create, write, fsync, rename with no waiting between
# the steps, so an hour is orders of magnitude past the longest plausible
# in-flight write and no temp file a running ghook could still rename falls
# inside it. The writer already removes its own temp on every error return
# (crates/ghook/src/transport.rs); this window exists for the one case the
# writer cannot handle, a killed process.
ORPHANED_TEMP_RETENTION_SECONDS: Final = 60 * 60.0

# Same bound and the same reason as the marker prune: one pass reads at most
# this many entries, so a backlog drains over successive passes while each pass
# stays short. The inbox holds tens of entries in steady state.
ORPHANED_TEMP_PRUNE_MAX_ENTRIES: Final = 100_000


def _compute_sleep_seconds(interval_seconds: int, jitter_seconds: float) -> float:
    """Return a non-negative poll interval with bounded jitter."""
    return max(
        0.0,
        interval_seconds + _JITTER_RANDOM.uniform(-jitter_seconds, jitter_seconds),
    )


def _is_orphaned_temp_name(name: str) -> bool:
    """True for ghook's atomic-write intermediate, never for a delivery-receipt claim.

    A claim holds an ack nobody has acknowledged yet; deleting it would lose the
    delivery, so a stranded claim is restored instead.
    """
    return name.endswith(".tmp") and not name.endswith(_RECEIPT_CLAIM_SUFFIX)


def prune_orphaned_inbox_temp_files(
    inbox_dir: Path | None = None,
    *,
    now: float | None = None,
    retention_seconds: float = ORPHANED_TEMP_RETENTION_SECONDS,
    max_entries: int = ORPHANED_TEMP_PRUNE_MAX_ENTRIES,
) -> DirectoryPruneResult:
    """Delete temp files a dead writer left behind, bounded to one pass.

    Blocking: the caller must keep this off the event loop thread. Only `.tmp`
    names are candidates, because a pending envelope shares this directory and
    is legitimately older than any window whenever the daemon was down.
    """
    target = inbox_dir if inbox_dir is not None else get_hook_inbox_dir()
    cutoff = (now if now is not None else time.time()) - retention_seconds
    return prune_directory_by_age(
        target,
        cutoff=cutoff,
        max_entries=max_entries,
        matches=_is_orphaned_temp_name,
    )


def _prune_hook_inbox_blocking(
    inbox_dir: Path,
) -> tuple[DirectoryPruneResult, DirectoryPruneResult]:
    """Run both retention passes in one worker-thread hop."""
    markers = prune_processed_envelope_markers(get_processed_envelope_dir(inbox_dir))
    temps = prune_orphaned_inbox_temp_files(inbox_dir)
    return markers, temps


async def prune_hook_inbox(inbox_dir: Path | None = None) -> int:
    """Drop expired markers and abandoned temp files, off the loop thread.

    Both passes stat every entry they read, so they share one worker-thread hop
    however small the directories currently are. Returns the total deleted.
    """
    root = inbox_dir or get_hook_inbox_dir()
    markers, temps = await asyncio.to_thread(_prune_hook_inbox_blocking, root)
    processed_dir = get_processed_envelope_dir(root)
    if markers.deleted:
        logger.info(
            "Pruned %s expired hook envelope marker(s) from %s",
            markers.deleted,
            processed_dir,
            extra={
                "event": "hook_envelope_markers_pruned",
                "examined": markers.examined,
                "deleted": markers.deleted,
                "backlog_remaining": markers.truncated,
            },
        )
    if temps.deleted:
        logger.info(
            "Reaped %s abandoned hook envelope temp file(s) from %s",
            temps.deleted,
            root,
            extra={
                "event": "hook_envelope_temp_files_reaped",
                "examined": temps.examined,
                "deleted": temps.deleted,
                "backlog_remaining": temps.truncated,
            },
        )
    return markers.deleted + temps.deleted


async def drain_hook_inbox_loop(
    app: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
    jitter_seconds: float = 5.0,
    prune_interval_seconds: float = 3600.0,
) -> None:
    """Background loop that replays pending hook inbox envelopes.

    The loop also owns inbox retention -- expired processed markers and temp
    files a dead writer abandoned both live in this directory, so pruning them
    here needs no second scheduled loop. Pruning runs on its own slower cadence
    because a drain has to be frequent and a prune does not.
    """
    try:
        replayed = await drain_hook_inbox_once(app)
        if replayed > 0:
            logger.debug("Hook inbox replayed %s pending envelope(s)", replayed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Initial hook inbox drain failed: %s", exc)

    next_prune_at = time.monotonic()
    while not is_shutdown_requested():
        try:
            sleep_seconds = _compute_sleep_seconds(interval_seconds, jitter_seconds)
            await asyncio.sleep(sleep_seconds)
            replayed = await drain_hook_inbox_once(app)
            if replayed > 0:
                logger.debug("Hook inbox replayed %s pending envelope(s)", replayed)
            if time.monotonic() >= next_prune_at:
                next_prune_at = time.monotonic() + prune_interval_seconds
                await prune_hook_inbox()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Hook inbox drain loop failed: %s", exc)
