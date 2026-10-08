"""Explicit host drain and process-boundary verification."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gobby.config.terminal_host import TerminalHostConfig
from gobby.terminals.host_client import HostCommandError, HostUnavailableError
from gobby.terminals.host_control import HostControlError
from gobby.terminals.host_protocol import read_pidfile

logger = logging.getLogger(__name__)


class HostShutdown(ABC):
    """Drain operations over the supervisor's existing process and client."""

    config: TerminalHostConfig
    host_pid: int | None
    last_error: str | None
    _client: Any
    _process: Any
    _pid_identity: Callable[[int], bool]

    @property
    @abstractmethod
    def socket_dir(self) -> Path:
        raise NotImplementedError

    @abstractmethod
    async def _connect(self) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def _handshake(self, client: Any) -> tuple[Any, Any]:
        raise NotImplementedError

    @abstractmethod
    async def _close_client(self, client: Any) -> None:
        raise NotImplementedError

    async def _host_shutdown(self) -> None:
        """Drain through the control socket, then verify exit and escalate signals.

        Explicit drain must also stop a live host that cannot be adopted (#22232).
        If connection or authentication fails, fall through to the pidfile and
        identity-checked SIGTERM/SIGKILL ladder instead of leaving terminals alive.
        """
        client = self._client
        pid = self.host_pid
        if client is None:
            try:
                client = await self._connect()
            except (OSError, ConnectionError, HostUnavailableError) as exc:
                logger.info("no gterm host answered the control socket: %s", exc)
                client = None
            if client is not None:
                try:
                    _, ping = await self._handshake(client)
                except (
                    OSError,
                    ConnectionError,
                    HostControlError,
                    HostCommandError,
                    PermissionError,
                ) as exc:
                    self.last_error = str(exc)
                    logger.warning("cannot authenticate to the gterm host to drain it: %s", exc)
                    await self._close_client(client)
                    client = None
                else:
                    self._client = client
                    pid = ping.host_pid
        grace_ms = int(self.config.shutdown_grace_seconds * 1000)
        if client is not None:
            try:
                await client.host_shutdown(grace_ms)
            except (ConnectionError, HostControlError, HostCommandError, OSError) as exc:
                logger.info("host_shutdown response lost; verifying death: %s", exc)
        if pid is None:
            pid = read_pidfile(self.socket_dir)
        if pid is None:
            return
        if await self._await_host_exit(pid):
            self.last_error = f"gterm host {pid} exited after host_shutdown"
            return

        previous_rung = "host_shutdown"
        for rung, host_signal in (("SIGTERM", signal.SIGTERM), ("SIGKILL", signal.SIGKILL)):
            if not self._pid_identity(pid):
                self.last_error = f"gterm host {pid} exited after {previous_rung}"
                return
            try:
                os.kill(pid, host_signal)
            except ProcessLookupError:
                self.last_error = f"gterm host {pid} exited after {previous_rung}"
                return
            except OSError as exc:
                logger.warning("could not send %s to gterm host %s: %s", rung, pid, exc)
            if await self._await_host_exit(pid):
                self.last_error = f"gterm host {pid} exited after {rung}"
                return
            previous_rung = rung

        self.last_error = f"gterm host {pid} is still running after SIGKILL"
        logger.warning(
            "gterm host %s did not exit after host_shutdown, SIGTERM, and SIGKILL",
            pid,
        )

    def _host_exit_deadline_seconds(self) -> float:
        return self.config.shutdown_grace_seconds

    async def _await_host_exit(self, pid: int) -> bool:
        """Poll for the host process boundary within the explicit drain deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._host_exit_deadline_seconds()
        while self._process_alive(pid):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def _process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _reap_process(self) -> None:
        process = self._process
        if process is None:
            return
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            self._process = None

    async def _drain_host(self) -> None:
        # The RPC reaches adopted hosts; reaping only applies to a spawned child.
        await self._host_shutdown()
        self._reap_process()
