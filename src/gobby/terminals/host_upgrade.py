"""Start in-place gterm host upgrades and hold the handover window.

Plan gterm-host-handover 2.1. When the adopted host runs an image other than
the installed gterm, the daemon asks it to exec the installed one in place.
While that attempt runs, the manager holds host death, restarts, reconcile,
and attaches; the host's own attempt record in ``ping`` closes the window.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gobby.terminals.host_client import (
    HostCommandError,
    HostDecodeError,
    HostUnavailableError,
    PingResult,
    UpgradeOutcome,
    UpgradeStatus,
)

# The host's attempt budget: it arms SIGALRM for this long when an attempt
# begins (crates/gterminal/src/host/upgrade.rs `BUDGET`).
HOST_BUDGET_SECONDS = 15.0
# Bounds the wait for the host's accept or refuse answer to `host_upgrade`.
REQUEST_TIMEOUT_SECONDS = 10.0
# The host cannot start an attempt right now; the next tick asks again.
_ASK_LATER = frozenset({"host_busy", "upgrade_in_progress", "host_draining"})

Probe = Callable[[], Awaitable[tuple[Any, Any, PingResult]]]


@dataclass
class UpgradeWindow:
    """One attempt the daemon holds host death, restart, and reconcile for."""

    attempt_id: str
    deadline: float
    host_pid: int
    host_epoch: str
    sha_at_start: str | None
    from_sha256: str | None = None
    candidate_sha256: str | None = None
    confirmed: bool = False


def _file_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


class HostUpgradeCoordinator:
    """Trigger upgrades of a stale host and track the window each one opens."""

    def __init__(
        self,
        *,
        installed: Callable[[], str | None],
        health_interval: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._installed = installed
        self._interval = health_interval
        self._monotonic = monotonic
        self.window: UpgradeWindow | None = None
        # In memory on purpose: a daemon restart or a new install tries once more.
        self._refused: set[str] = set()
        self._finished: set[str] = set()
        self._hash_key: tuple[int, int] | None = None
        self._hash: str | None = None
        self._closed = asyncio.Event()
        self._closed.set()

    @property
    def is_open(self) -> bool:
        return self.window is not None

    def expired(self) -> bool:
        return self.window is not None and self._monotonic() >= self.window.deadline

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def observe(self, client: Any, ping: PingResult, capabilities: tuple[str, ...]) -> None:
        """Fold one healthy ping into the window, or ask a stale host to upgrade."""
        if "host_upgrade" not in capabilities:
            return
        status = ping.upgrade
        if status is None:
            return
        if status.phase != "idle":
            await self._track(ping, status)
            return
        if self.window is not None:
            self._close_on_outcome(ping)
            return
        installed = await self._installed_sha256()
        if installed is None or installed == ping.binary_sha256 or installed in self._refused:
            return
        await self._request(client, ping, installed)

    async def settle_expired(self, probe: Probe) -> tuple[Any, Any, PingResult] | None:
        """Decide a window past its deadline with one fresh ``hello`` and ``ping``.

        Returns the probe's client, hello, and ping when the host is fine, or
        None when the check failed and the caller falls through to host death.
        """
        window = self.window
        if window is None:
            return None
        try:
            fresh = await probe()
        except Exception:
            self._close()
            return None
        client, _hello, ping = fresh
        if (ping.host_pid, ping.host_epoch) != (window.host_pid, window.host_epoch):
            self._close()
            await client.close()
            return None
        if self._close_on_outcome(ping):
            return fresh
        self._close()
        if ping.upgrade is not None and ping.upgrade.phase == "idle":
            return fresh
        # The host arms its alarm when an attempt begins, so a conforming host
        # cannot still be mid-attempt past the deadline.
        await client.close()
        return None

    async def _track(self, ping: PingResult, status: UpgradeStatus) -> None:
        attempt_id = status.attempt_id
        if attempt_id is None or attempt_id in self._finished:
            return
        window = self.window
        if window is None or window.attempt_id != attempt_id:
            # A daemon that restarted mid-upgrade adopts the host's attempt.
            window = self._open(
                attempt_id,
                ping,
                sha_at_start=await self._installed_sha256(),
                budget=HOST_BUDGET_SECONDS,
            )
        self._confirm(window, status.candidate_sha256, status.remaining_ms)

    async def _request(self, client: Any, ping: PingResult, installed: str) -> None:
        attempt_id = uuid.uuid4().hex
        # Opened before sending: its deadline bounds any attempt this request starts.
        window = self._open(
            attempt_id,
            ping,
            sha_at_start=installed,
            budget=REQUEST_TIMEOUT_SECONDS + HOST_BUDGET_SECONDS,
        )
        try:
            reply = await asyncio.wait_for(
                client.host_upgrade(self._installed(), attempt_id), REQUEST_TIMEOUT_SECONDS
            )
        except (
            HostUnavailableError,
            HostDecodeError,
            ConnectionError,
            OSError,
            TimeoutError,
        ):
            return
        except HostCommandError as exc:
            self._close()
            if exc.code in _ASK_LATER:
                return
            # `upgrade_refused` names no candidate: the installed image is refused.
            self._refused.add(installed)
            return
        remaining_ms = reply.get("remaining_ms")
        self._confirm(
            window,
            reply.get("candidate_sha256"),
            remaining_ms if isinstance(remaining_ms, int) else None,
        )

    def _open(
        self, attempt_id: str, ping: PingResult, *, sha_at_start: str | None, budget: float
    ) -> UpgradeWindow:
        if self.window is not None:
            self._finished.add(self.window.attempt_id)
        self.window = UpgradeWindow(
            attempt_id=attempt_id,
            deadline=self._monotonic() + budget + self._interval,
            host_pid=ping.host_pid,
            host_epoch=ping.host_epoch,
            sha_at_start=sha_at_start,
            from_sha256=ping.binary_sha256,
        )
        self._closed.clear()
        return self.window

    def _confirm(self, window: UpgradeWindow, candidate: Any, remaining_ms: int | None) -> None:
        window.confirmed = True
        if isinstance(candidate, str) and candidate:
            window.candidate_sha256 = candidate
        if remaining_ms is not None:
            # Only ever shortened: a host reporting 0 ms left cannot hold it open.
            window.deadline = min(
                window.deadline, self._monotonic() + remaining_ms / 1000 + self._interval
            )

    def _close_on_outcome(self, ping: PingResult) -> bool:
        window = self.window
        outcome = ping.upgrade.last_outcome if ping.upgrade is not None else None
        if window is None or outcome is None or outcome.attempt_id != window.attempt_id:
            return False
        self._judge(window, outcome, ping.binary_sha256)
        self._close()
        return True

    def _judge(self, window: UpgradeWindow, outcome: UpgradeOutcome, running: str | None) -> None:
        candidate = outcome.candidate_sha256 or window.candidate_sha256 or window.sha_at_start
        if outcome.outcome == "deferred":
            return
        if outcome.outcome == "succeeded" and running is not None and running == candidate:
            return
        if candidate is not None:
            self._refused.add(candidate)

    def _close(self) -> None:
        if self.window is not None:
            self._finished.add(self.window.attempt_id)
        self.window = None
        self._closed.set()

    async def _installed_sha256(self) -> str | None:
        path = self._installed()
        if not path:
            return None
        try:
            stat = os.stat(path)
            key = (stat.st_ino, stat.st_mtime_ns)
            if key != self._hash_key:
                self._hash = await asyncio.to_thread(_file_sha256, path)
                self._hash_key = key
        except OSError:
            return None
        return self._hash


__all__ = [
    "HOST_BUDGET_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "HostUpgradeCoordinator",
    "UpgradeWindow",
]
