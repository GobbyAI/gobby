"""Supervise the gterm host process: adopt, spawn, health, and crash."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any

from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import ATTACH_HISTORY_LINES
from gobby.storage.terminals import TerminalManager
from gobby.terminals.host_client import HostClient, HostCommandError, HostManagerStopped
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


class _Adopt(Enum):
    """Outcome of probing the control socket for a surviving host."""

    ADOPTED = "adopted"
    ABSENT = "absent"
    MISMATCH = "mismatch"


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
        tmux_attach_history_lines: int = ATTACH_HISTORY_LINES,
    ) -> None:
        self.config = config
        self.terminal_config = terminal_config
        # The operator's `tmux.attach_history_lines`; the host applies it to
        # every tmux observer's AttachHistory frame (#20815).
        self.tmux_attach_history_lines = tmux_attach_history_lines
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
        self._restart_task: asyncio.Task[str] | None = None
        self._restart_lock = asyncio.Lock()
        self._restart_generation = 0
        self._restart_failures = 0
        self._healthy_since: float | None = None
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._monotonic: Callable[[], float] = time.monotonic
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
        # A surviving host we could reach but not adopt (protocol, token, or
        # pid identity disagreed). It stays alive and untouched; native launches
        # degrade until the operator drains it (#22002).
        self.host_mismatch: str | None = None
        # Set once `stop(drain_host=True)` took the host down, so the child
        # reaper no longer holds its pid back.
        self.host_drained = False
        self._stop_requested = False
        # Set once `start()` has finished, adopted, spawned, or degraded, so
        # attach paths that race daemon startup wait for a decided host
        # instead of handshaking against an unset epoch (#22002).
        self._startup_settled = asyncio.Event()
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
            "host_mismatch": self.host_mismatch,
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
        async with self._restart_lock:
            self._stop_requested = False
            self.host_drained = False
        try:
            await self._start_host()
        finally:
            self._startup_settled.set()

    async def wait_startup_settled(self, timeout: float) -> bool:
        """True once ``start()`` has decided the host; False if that takes longer."""
        try:
            await asyncio.wait_for(self._startup_settled.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def _start_host(self) -> None:
        if not self.enabled:
            self.native_available = False
            return
        try:
            outcome = await self._try_adopt()
            if outcome is _Adopt.ADOPTED:
                self.native_available = True
                self.running = True
                self.last_error = None
                await self.reconcile()
                self._healthy_since = self._monotonic()
                self._arm_health()
                return
            if outcome is _Adopt.MISMATCH:
                # Never race a live host: no second spawn, no token rotation.
                self.running = False
                self.native_available = False
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

    async def stop(self, *, drain_host: bool = False) -> None:
        """Detach from the host; drain it only on explicit opt-in.

        The default leaves the gterm host and every terminal it owns running so
        the next daemon adopts them. ``drain_host`` (or the
        ``terminals.stop_host_on_shutdown`` config) takes the host down too.
        """
        async with self._restart_lock:
            self._stop_requested = True
            self._restart_generation += 1
            restart_task = self._restart_task
            self._restart_task = None
            if restart_task is not None:
                restart_task.cancel()
        if restart_task is not None:
            try:
                await restart_task
            except (asyncio.CancelledError, HostManagerStopped):
                pass
        await self.stop_producers()
        if not (drain_host or self.terminal_config.stop_host_on_shutdown):
            await self.close_clients()
            return
        self.host_drained = True
        await self._drain_host()
        await self.close_clients()
        self.running = False
        self.host_pid = None

    async def ensure_restart(self) -> str:
        """Return one shared restart epoch, fenced against teardown and drain."""
        async with self._restart_lock:
            if self._stop_requested or self.host_drained or not self.enabled:
                raise HostManagerStopped("gterm host manager stopped")
            task = self._restart_task
            if task is None or task.done():
                self._restart_generation += 1
                generation = self._restart_generation
                task = asyncio.create_task(
                    self._restart_host(generation),
                    name="gterm-host-restart",
                )
                self._restart_task = task
        return await asyncio.shield(task)

    async def _restart_host(self, generation: int) -> str:
        current = asyncio.current_task()
        try:
            while self._restart_failures < self.config.restart_max_attempts:
                delay = self.backoff_seconds or 1.0
                await self._sleep(delay)
                async with self._restart_lock:
                    if (
                        self._stop_requested
                        or self.host_drained
                        or self._restart_task is not current
                        or self._restart_generation != generation
                    ):
                        raise HostManagerStopped("gterm host manager stopped")
                try:
                    candidate = await self._spawn_candidate()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._restart_failures += 1
                    self.backoff_seconds = min(
                        delay * 2,
                        self.config.restart_backoff_ceiling_seconds,
                    )
                    self.last_error = str(exc)
                    continue

                client, hello, ping = candidate
                async with self._restart_lock:
                    publish = (
                        not self._stop_requested
                        and not self.host_drained
                        and self._restart_task is current
                        and self._restart_generation == generation
                    )
                    if publish:
                        self._publish_spawned_client(client, hello, ping)
                        self.backoff_seconds = delay
                        self._healthy_since = self._monotonic()
                if not publish:
                    await self._close_client(client)
                    raise HostManagerStopped("gterm host manager stopped")
                self.native_available = True
                self.running = True
                self.last_error = None
                await self.reconcile()
                return str(self.host_epoch or "")

            self.running = False
            self.native_available = False
            raise HostManagerStopped("gterm host restart attempts exhausted")
        except asyncio.CancelledError as exc:
            if self._stop_requested or self.host_drained:
                raise HostManagerStopped("gterm host manager stopped") from exc
            raise
        finally:
            async with self._restart_lock:
                if self._restart_task is current and self._restart_generation == generation:
                    self._restart_task = None

    def preserved_host_pid(self) -> int | None:
        """Identity-checked host pid the shutdown reaper must leave alone."""
        if self.host_drained:
            return None
        pid = self.host_pid or read_pidfile(self.socket_dir)
        if pid and self._pid_identity(pid):
            return pid
        return None

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
        self._healthy_since = None
        if manager is not None:
            machine_id = require_machine_id()
            rows = [
                row for row in manager.list_live_by_machine(machine_id) if row.backend == "native"
            ]
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
        if self._stop_requested or self.host_drained:
            return
        await self.ensure_restart()

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
        pending = manager.get(terminal_id)
        if pending is None:
            return
        recorded = manager.record_process(
            terminal_id,
            {
                "host_terminal_id": str(event["host_terminal_id"]),
                "pgid": event["pgid"],
                "start_time": event["start_time"],
            },
            attempt_generation=pending.attempt_generation,
            attempt_started_at=pending.attempt_started_at,
        )
        if recorded is None:
            return
        client = self._client
        if client is None:
            # A connection this daemon just opened is unauthenticated, and the
            # host refuses `spawn_commit` on one; handshake before commanding,
            # and publish the client only once it is usable (#22232).
            client = await self._connect()
            await self._handshake(client)
            self._client = client
        await client.spawn_commit(terminal_id, spawn_key, self.config.commit_deadline_ms)

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

        try:
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
        except ValueError as exc:
            logger.warning("host inventory reconcile skipped invalid identity: %s", exc)
            self.last_error = str(exc)
            self._record_observation_health(host_rows)
            return
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

    async def _try_adopt(self) -> _Adopt:
        """Probe the control socket; adopt a matching host, never disturb one."""
        socket_path = control_socket_path(self.socket_dir)
        if self._connector is None and not socket_path.exists():
            return _Adopt.ABSENT
        try:
            client = await self._connect()
        except (OSError, ConnectionError) as exc:
            # Nobody is listening: a stale socket file, not a live host.
            self.last_error = str(exc)
            return _Adopt.ABSENT
        self.host_mismatch = None
        try:
            hello, ping = await self._handshake(client)
            if not pid_matches_ping(
                socket_dir=self.socket_dir,
                host_pid=ping.host_pid,
                identity=self._pid_identity,
            ):
                raise HostControlError(
                    f"host pid {ping.host_pid} does not match the pidfile or a live gterm"
                )
        except (
            OSError,
            HostControlError,
            HostCommandError,
            PermissionError,
            ConnectionError,
        ) as exc:
            await self._close_client(client)
            self.host_mismatch = str(exc)
            self.last_error = str(exc)
            logger.warning(
                "gterm host at %s is alive but not adoptable (%s); leaving it and its "
                "terminals running. Drain it with `gobby stop --terminals` to replace it.",
                socket_path,
                exc,
            )
            return _Adopt.MISMATCH
        self._client = client
        self.host_epoch = ping.host_epoch or hello.host_epoch
        self.host_pid = ping.host_pid
        self.protocol_version = int(hello.protocol_version)
        self.adopted = True
        self.spawned_this_construction = False
        return _Adopt.ADOPTED

    async def _spawn_and_connect(self) -> None:
        client, hello, ping = await self._spawn_candidate()
        self._publish_spawned_client(client, hello, ping)

    async def _spawn_candidate(self) -> tuple[Any, Any, Any]:
        # Only reached when nothing answered the control socket, so a fresh
        # token cannot lock out a live host.
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
        return client, hello, ping

    def _publish_spawned_client(self, client: Any, hello: Any, ping: Any) -> None:
        self._client = client
        self.host_epoch = ping.host_epoch or hello.host_epoch
        self.host_pid = ping.host_pid
        self.protocol_version = int(hello.protocol_version)
        self.adopted = False
        self.spawned_this_construction = True
        self.restart_count += 1
        self._healthy_since = self._monotonic()

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
                    "--max-attachments-total",
                    str(self.config.max_attachments_total),
                    "--max-attachments-per-terminal",
                    str(self.config.max_attachments_per_terminal),
                    "--tmux-attach-history-lines",
                    str(self.tmux_attach_history_lines),
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

    async def _handshake(self, client: Any) -> tuple[Any, Any]:
        """Authenticate a fresh control connection and probe it.

        The host answers every verb but ``hello`` with ``unauthenticated`` and
        hangs up, so a connection this daemon just opened is useless until this
        runs. Returns the hello and ping payloads.
        """
        token = self.ensure_control_token()
        hello = await client.hello(CONTROL_PROTOCOL_VERSION, token)
        if int(hello.protocol_version) < CONTROL_PROTOCOL_VERSION:
            raise HostControlError(
                f"host speaks control protocol {hello.protocol_version}, "
                f"daemon needs {CONTROL_PROTOCOL_VERSION}"
            )
        return hello, await client.ping()

    async def _host_shutdown(self) -> None:
        """Drain the host, escalating through TERM and KILL when needed.

        ``self._client`` is already authenticated when this daemon adopted or
        spawned the host. When it is not — the host is alive but was not
        adoptable, which is the case `gobby stop --terminals` exists for — the
        connection opened here has to handshake first, or the host refuses the
        shutdown as ``unauthenticated`` and the drain silently does nothing
        (#22232).
        """
        client = self._client
        pid = self.host_pid
        if client is None:
            try:
                client = await self._connect()
            except (OSError, ConnectionError) as exc:
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
        """Poll for ``pid`` to leave before the drain gives up on it."""
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

    async def _close_client(self, client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def _drain_host(self) -> None:
        # Both rungs run for every host: the RPC is the only one that reaches a
        # host this daemon did not spawn, and `_reap_process` is a no-op unless
        # it did. The old name read as a condition and is why #22232 was first
        # diagnosed as a drain that skips adopted hosts.
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

    def _record_healthy_ping(self) -> None:
        now = self._monotonic()
        if self._healthy_since is None:
            self._healthy_since = now
            return
        if now - self._healthy_since >= 60.0:
            self.backoff_seconds = 0.0
            self._restart_failures = 0

    async def _health_loop(self) -> None:
        interval = self.config.health_interval_seconds
        while not self._stop_requested:
            await self._sleep(interval)
            client = self._client
            if client is None:
                continue
            try:
                ping = await client.ping()
                self.host_pid = ping.host_pid
                self.host_epoch = ping.host_epoch
                await self.reconcile()
                self._record_healthy_ping()
            except Exception as exc:
                self.last_error = str(exc)
                pid = self.host_pid
                if isinstance(pid, int) and pid > 0 and self._pid_identity(pid):
                    logger.warning("gterm control probe failed; reconnecting live host: %s", exc)
                    try:
                        stale = self._client
                        if stale is not None:
                            close = getattr(stale, "close", None)
                            if callable(close):
                                result = close()
                                if asyncio.iscoroutine(result):
                                    await result
                        replacement = await self._connect()
                        token = self.ensure_control_token()
                        hello = await replacement.hello(CONTROL_PROTOCOL_VERSION, token)
                        ping = await replacement.ping()
                        self._client = replacement
                        self.host_epoch = ping.host_epoch or hello.host_epoch
                        self.host_pid = ping.host_pid
                    except Exception as reconnect_exc:
                        self.last_error = str(reconnect_exc)
                        try:
                            await self.handle_host_death()
                        except HostManagerStopped:
                            return
                        if self.running:
                            continue
                        return
                    continue
                try:
                    await self.handle_host_death()
                except HostManagerStopped:
                    return
                if self.running:
                    continue
                return


# Re-export for tests that import the filename from host_protocol via manager.
__all__ = ["CONTROL_TOKEN_FILE_NAME", "TerminalHostManager"]
