"""Supervise the gterm host process: adopt, spawn, health, and crash."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.storage.terminals import TerminalManager
from gobby.terminals.host_client import HostClient, HostCommandError
from gobby.terminals.host_control import HostControlError
from gobby.terminals.host_identity import PidIdentity, is_live_gterm, pid_matches_ping
from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    CONTROL_TOKEN_FILE_NAME,
    HOST_LOG_NAME,
    control_socket_path,
    control_token_path,
    expand_socket_dir,
    read_pidfile,
    write_pidfile,
)
from gobby.terminals.host_reap import reap_recorded_process
from gobby.terminals.host_reconcile import reconcile_host_inventory
from gobby.utils.machine_id import require_machine_id
from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

Connector = Callable[[], Awaitable[Any]]
Spawner = Callable[[], Any]


class TerminalHostManager:
    """Owns gterm lifecycle for one daemon composition root."""

    def __init__(
        self,
        *,
        config: TerminalHostConfig,
        terminal_config: TerminalConfig,
        terminal_manager: TerminalManager | None = None,
        run_manager: Any | None = None,
        connector: Connector | None = None,
        spawner: Spawner | None = None,
        pid_identity: PidIdentity | None = None,
    ) -> None:
        self.config = config
        self.terminal_config = terminal_config
        self.terminal_manager = terminal_manager
        self.run_manager = run_manager
        self._connector = connector
        self._spawner = spawner
        self._pid_identity = pid_identity or is_live_gterm
        self._client: Any | None = None
        self._frame_client: Any | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._process: Any | None = None
        self._health_task: asyncio.Task[None] | None = None
        self.enabled = config.enabled
        self.running = False
        self.adopted = False
        self.spawned_this_construction = False
        self.native_available = False
        self.host_epoch: str | None = None
        self.protocol_version: int | None = CONTROL_PROTOCOL_VERSION
        self.host_pid: int | None = None
        self.restart_count = 0
        self.backoff_seconds = 0.0
        self.last_error: str | None = None
        self._stop_requested = False
        self.observation_health: dict[str, dict[str, Any]] = {}

    @property
    def socket_dir(self) -> Path:
        return expand_socket_dir(self.config.socket_dir)

    def health_state(self) -> dict[str, Any]:
        live = 0
        orphaned = 0
        manager = self.terminal_manager
        epoch = self.host_epoch
        if manager is not None and epoch is not None:
            live = len(manager.list_live_by_epoch(epoch))
            orphaned = len(manager.list_orphaned_by_epoch(epoch))
        return {
            "enabled": self.enabled,
            "running": self.running,
            "adopted": self.adopted,
            "host_epoch": self.host_epoch,
            "protocol_version": self.protocol_version,
            "restart_count": self.restart_count,
            "backoff_seconds": self.backoff_seconds,
            "live_terminals": live,
            "orphaned_terminals": orphaned,
            "last_error": self.last_error,
        }

    def ensure_control_token(self) -> str:
        path = control_token_path(self.socket_dir)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        token = secrets.token_urlsafe(32)
        self._write_token(token)
        return token

    def rotate_control_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._write_token(token)
        return token

    def _write_token(self, token: str) -> None:
        from gobby.terminals.host_protocol import atomic_replace_text

        atomic_replace_text(control_token_path(self.socket_dir), token, 0o600)

    async def start(self) -> None:
        if not self.enabled:
            self.native_available = False
            return
        self._stop_requested = False
        try:
            if await self._try_adopt():
                self.native_available = True
                self.running = True
                self.last_error = None
                await self.reconcile()
                self._arm_health()
                return
            await self._spawn_and_connect()
            self.native_available = True
            self.running = True
            self.last_error = None
            await self.reconcile()
            self._arm_health()
        except Exception as exc:
            self.running = False
            self.native_available = False
            self.last_error = str(exc)
            logger.warning("gterm host unavailable; native launches degraded: %s", exc)

    async def stop(self, *, preserve_host: bool) -> None:
        self._stop_requested = True
        await self.stop_producers()
        if preserve_host:
            await self.close_clients()
            return
        await self._drain_if_spawned_or_stop()
        await self.close_clients()
        self.running = False
        self.host_pid = None

    async def stop_producers(self) -> None:
        tasks = [self._health_task, self._reconnect_task]
        self._health_task = None
        self._reconnect_task = None
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def rollback_host(self) -> None:
        if self.spawned_this_construction and not self.adopted:
            await self._host_shutdown()
            self._reap_process()
        # Adopted hosts stay up.

    async def close_clients(self) -> None:
        client = self._client
        frame = self._frame_client
        self._client = None
        self._frame_client = None
        self._reconnect_task = None
        for item in (client, frame):
            if item is None:
                continue
            close = getattr(item, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def handle_host_death(self) -> None:
        epoch = self.host_epoch
        manager = self.terminal_manager
        self.running = False
        self.native_available = False
        if manager is None:
            return
        machine_id = require_machine_id()
        rows = [row for row in manager.list_live_by_machine(machine_id) if row.backend == "native"]
        for row in rows:
            if epoch is not None and row.host_epoch not in {None, epoch}:
                manager.mark_orphaned(row.id)
                self._interrupt(row.agent_run_id)
                if row.process:
                    self.reap_recorded_process(row.process)
                continue
            if row.state == "live":
                manager.mark_orphaned(row.id)
                self._interrupt(row.agent_run_id)
            if row.process:
                self.reap_recorded_process(row.process)

    def reap_recorded_process(self, process: Any) -> None:
        if not isinstance(process, dict):
            return
        reap_recorded_process(process, grace_seconds=self.config.shutdown_grace_seconds)

    async def handle_spawn_prepared(self, event: dict[str, Any]) -> None:
        manager = self.terminal_manager
        if manager is None:
            return
        terminal_id = str(event["terminal_id"])
        spawn_key = str(event["spawn_key"])
        recorded = manager.record_process(
            terminal_id,
            {"pgid": event["pgid"], "start_time": event["start_time"]},
        )
        if recorded is None:
            return
        client = self._client
        if client is None:
            client = await self._connect()
            self._client = client
        await client.spawn_commit(terminal_id, spawn_key)

    async def reconcile(self) -> None:
        manager = self.terminal_manager
        if manager is None:
            return
        client = self._client
        host_rows: list[Any] = []
        if client is not None:
            try:
                host_rows = list(await client.list_terminals())
            except Exception as exc:
                self.last_error = str(exc)
                return
        epoch = self.host_epoch or ""

        async def kill(host_terminal_id: str) -> None:
            if client is None:
                return
            await client.kill(host_terminal_id)

        error = await reconcile_host_inventory(
            terminal_manager=manager,
            machine_id=require_machine_id(),
            host_epoch=epoch,
            host_rows=host_rows,
            spawn_in_doubt_seconds=self.terminal_config.spawn_in_doubt_seconds,
            run_manager=self.run_manager,
            kill=kill,
            unknown_grace_seconds=self.config.shutdown_grace_seconds,
        )
        if error:
            self.last_error = error
        self._record_observation_health(host_rows)

    def _record_observation_health(self, host_rows: list[Any]) -> None:
        manager = self.terminal_manager
        if manager is None:
            return
        for row in host_rows:
            state = str(getattr(row, "observation_state", "live") or "live")
            reason = getattr(row, "observation_reason", None)
            generation = int(getattr(row, "observation_generation", 1) or 1)
            terminal_id = str(row.terminal_id)
            self.observation_health[terminal_id] = {
                "observation_state": state,
                "observation_reason": reason,
                "observation_generation": generation,
            }
            if state in {"stale", "orphaned_observation"}:
                continue
            if state == "live":
                continue

    def note_confirmed_absence(self, terminal_id: str) -> None:
        manager = self.terminal_manager
        if manager is None:
            return
        manager.mark_exited(terminal_id)
        self.observation_health.pop(terminal_id, None)

    async def _try_adopt(self) -> bool:
        socket_path = control_socket_path(self.socket_dir)
        if self._connector is None and not socket_path.exists():
            return False
        try:
            client = await self._connect()
            token = self.ensure_control_token()
            hello = await client.hello(CONTROL_PROTOCOL_VERSION, token)
            if int(hello.protocol_version) < CONTROL_PROTOCOL_VERSION:
                await self._shutdown_client(client)
                self.rotate_control_token()
                return False
            ping = await client.ping()
            if not pid_matches_ping(
                socket_dir=self.socket_dir,
                host_pid=ping.host_pid,
                identity=self._pid_identity,
            ):
                await self._shutdown_client(client)
                self.rotate_control_token()
                return False
            self._client = client
            self.host_epoch = ping.host_epoch or hello.host_epoch
            self.host_pid = ping.host_pid
            self.protocol_version = int(hello.protocol_version)
            self.adopted = True
            self.spawned_this_construction = False
            return True
        except (
            OSError,
            HostControlError,
            HostCommandError,
            PermissionError,
            ConnectionError,
        ) as exc:
            self.last_error = str(exc)
            return False

    async def _spawn_and_connect(self) -> None:
        stale = self._client
        if stale is not None:
            await self._shutdown_client(stale)
            self._client = None
        elif control_socket_path(self.socket_dir).exists():
            try:
                client = await self._connect()
                await self._shutdown_client(client)
            except Exception:
                logger.debug("Could not drain stale host before spawn", exc_info=True)
        self.rotate_control_token() if control_token_path(self.socket_dir).exists() else (
            self.ensure_control_token()
        )
        token = self.ensure_control_token()
        process = self._spawn_host_process()
        self._process = process
        pid = int(getattr(process, "pid", 0) or 0)
        if pid:
            write_pidfile(self.socket_dir, pid)
        client = await self._wait_for_client()
        hello = await client.hello(CONTROL_PROTOCOL_VERSION, token)
        ping = await client.ping()
        self._client = client
        self.host_epoch = ping.host_epoch or hello.host_epoch
        self.host_pid = ping.host_pid
        self.protocol_version = int(hello.protocol_version)
        self.adopted = False
        self.spawned_this_construction = True
        self.restart_count += 1

    def _spawn_host_process(self) -> Any:
        if self._spawner is not None:
            spawned = self._spawner()
            if spawned is None:
                raise FileNotFoundError("gterm")
            return spawned
        binary = self.config.binary_path or resolve_native_bin("gterm")
        if not binary:
            raise FileNotFoundError("gterm")
        log_dir = self.socket_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / HOST_LOG_NAME
        env = os.environ.copy()
        env["GTERM_LOG_FILE"] = str(log_path)
        with log_path.open("a", encoding="utf-8") as log_file:
            return __import__("subprocess").Popen(  # nosec B603
                [
                    binary,
                    "host",
                    "--socket-dir",
                    str(self.socket_dir),
                    "--tmux-poll-interval-ms",
                    str(self.config.tmux_poll_interval_ms),
                    "--tmux-poll-backoff-ceiling-ms",
                    str(self.config.tmux_poll_backoff_ceiling_ms),
                    "--max-attached-terminals",
                    str(self.config.max_attached_terminals),
                    "--tmux-attach-history-lines",
                    str(self.config.tmux_attach_history_lines),
                    "--tmux-attach-history-max-bytes",
                    str(self.config.tmux_attach_history_max_bytes),
                ],
                stdin=__import__("subprocess").DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                env=env,
            )

    async def _wait_for_client(self) -> Any:
        deadline = asyncio.get_running_loop().time() + 5.0
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await self._connect()
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.05)
        raise ConnectionError(f"gterm control socket never appeared: {last_error}")

    async def _connect(self) -> Any:
        if self._connector is not None:
            return await self._connector()
        return await HostClient.connect(control_socket_path(self.socket_dir))

    async def _host_shutdown(self) -> None:
        client = self._client
        if client is None:
            try:
                client = await self._connect()
                self._client = client
            except Exception:
                return
        grace_ms = int(self.config.shutdown_grace_seconds * 1000)
        try:
            await client.host_shutdown(grace_ms)
        except (ConnectionError, HostControlError, HostCommandError, OSError) as exc:
            logger.info("host_shutdown response lost; verifying death: %s", exc)
        pid = self.host_pid or read_pidfile(self.socket_dir)
        if pid and not self._process_alive(pid):
            return

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
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    async def _shutdown_client(self, client: Any) -> None:
        shutdown = getattr(client, "host_shutdown", None)
        if callable(shutdown):
            try:
                result = shutdown(int(self.config.shutdown_grace_seconds * 1000))
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("stale host_shutdown failed", exc_info=True)
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def _drain_if_spawned_or_stop(self) -> None:
        await self._host_shutdown()
        self._reap_process()

    def _interrupt(self, run_id: str | None) -> None:
        if not run_id or self.run_manager is None:
            return
        cancel = getattr(self.run_manager, "cancel", None)
        if callable(cancel):
            cancel(run_id, terminal_reason="daemon_stop")

    def _arm_health(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._health_task is not None:
            return
        self._health_task = loop.create_task(self._health_loop(), name="gterm-host-health")

    async def _health_loop(self) -> None:
        interval = self.config.health_interval_seconds
        while not self._stop_requested:
            await asyncio.sleep(interval)
            client = self._client
            if client is None:
                continue
            try:
                ping = await client.ping()
                self.host_pid = ping.host_pid
                self.host_epoch = ping.host_epoch
            except Exception as exc:
                self.last_error = str(exc)
                await self.handle_host_death()
                return


# Re-export for tests that import the filename from host_protocol via manager.
__all__ = ["CONTROL_TOKEN_FILE_NAME", "TerminalHostManager"]
