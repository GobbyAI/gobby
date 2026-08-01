"""Managed binary freshness maintenance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from random import SystemRandom
from typing import TYPE_CHECKING, Any

from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.runner_maintenance_helpers import _run_db

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger("gobby.runner_maintenance")
_JITTER_RANDOM = SystemRandom()


async def _sleep_until_next_bin_freshness_cycle(
    duration: float,
    *,
    is_shutdown_requested: Callable[[], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if duration <= 0 or is_shutdown_requested():
        return
    await sleep(duration)


async def bin_freshness_loop(
    db: HubDatabase,
    config: BinFreshnessConfig,
    is_shutdown_requested: Callable[[], bool],
    *,
    update_once: Callable[[HubDatabase, BinFreshnessConfig], list[Any]] | None = None,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float], float] | None = None,
) -> None:
    """Background loop for GitHub-backed managed native binary updates."""
    if not config.enabled:
        return

    from gobby.install.bin_freshness_updater import update_all_managed_bins

    updater = update_once or update_all_managed_bins
    jitter_fn = jitter or (lambda upper: _JITTER_RANDOM.uniform(0, upper))

    try:
        await _sleep_until_next_bin_freshness_cycle(
            config.initial_delay_seconds,
            is_shutdown_requested=is_shutdown_requested,
            sleep=sleep,
        )
        while not is_shutdown_requested():
            try:
                await _run_db(run_db, updater, db, config)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in bin freshness loop: %s", e)

            interval = config.interval_seconds
            if config.jitter_seconds > 0:
                interval += jitter_fn(config.jitter_seconds)
            try:
                await _sleep_until_next_bin_freshness_cycle(
                    interval,
                    is_shutdown_requested=is_shutdown_requested,
                    sleep=sleep,
                )
            except asyncio.CancelledError:
                break
    except asyncio.CancelledError:
        pass
