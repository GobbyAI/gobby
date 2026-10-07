"""Interim gdaemon ownership: the runner spawns `gdaemon serve` in front of its backend pair.

With `front_door.enabled`, gdaemon owns the public HTTP and WS ports and proxies to
the runner's loopback backend pair. Until gdaemon becomes the supervisor (plan 5.2,
which deletes this module), the runner is gdaemon's only owner, so a service launch
and a direct launch get the same child.

The child inherits exactly one descriptor: the read end of a liveness pipe named in
`GOBBY_PARENT_FD`. The runner holds the write end, so the child sees EOF and exits
however the runner dies, SIGKILL included.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import subprocess  # nosec B404 # the runner owns its gdaemon child
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from gobby.config.bootstrap import BootstrapConfig, backend_ports
from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

PARENT_FD_ENV = "GOBBY_PARENT_FD"
BACKEND_HOST = "127.0.0.1"
STARTUP_WINDOW_SECONDS = 10.0
PORT_REUSE_WAIT_SECONDS = 10.0
STOP_GRACE_SECONDS = 5.0
RESPAWN_INITIAL_BACKOFF_SECONDS = 1.0
RESPAWN_MAX_BACKOFF_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.1
_WILDCARD_HOSTS = {"0.0.0.0", "::", ""}  # nosec B104 # normalized for probes, never bound


class FrontDoorStartupError(RuntimeError):
    """gdaemon could not take the public ports, so the runner must not start."""


@dataclass(frozen=True)
class BackendBind:
    """Where the runner's HTTP and WS servers bind."""

    host: str
    http_port: int
    ws_port: int


def backend_bind(bootstrap: BootstrapConfig) -> BackendBind:
    """The loopback backend pair behind the front door, else the public pair on bind_host."""
    if bootstrap.front_door.enabled:
        http_port, ws_port = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)
        return BackendBind(BACKEND_HOST, http_port, ws_port)
    return BackendBind(bootstrap.bind_host, bootstrap.daemon_port, bootstrap.websocket_port)


def _bindable(host: str, port: int) -> bool:
    # The family gdaemon serve and uvicorn bind: IPv6 only for an IPv6 literal.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _accepts(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class FrontDoorChild:
    """One runner's `gdaemon serve` child: spawn, readiness, respawn, and stop."""

    def __init__(
        self, *, binary: str, bind_host: str, public_ports: tuple[int, int], bootstrap_dir: Path
    ) -> None:
        self._binary = binary
        self._bind_host = bind_host
        self._public_ports = public_ports
        self._bootstrap_dir = bootstrap_dir
        self.secret = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._write_fd: int | None = None
        self._disarmed = False
        self._monitor: asyncio.Task[None] | None = None

    @classmethod
    def from_bootstrap(
        cls, bootstrap: BootstrapConfig, bootstrap_dir: Path
    ) -> FrontDoorChild | None:
        """The child this bootstrap asks for, or None when the front door is disabled.

        gdaemon reads `bootstrap.yaml` from `GOBBY_HOME`, so `bootstrap_dir` is the
        directory the runner loaded its bootstrap from.
        """
        if not bootstrap.front_door.enabled:
            return None
        binary = resolve_native_bin("gdaemon")
        if binary is None:
            raise FrontDoorStartupError(
                "front_door.enabled is true but no gdaemon binary was found; run "
                "`gobby install` to restore the front door required for API-key authentication"
            )
        return cls(
            binary=binary,
            bind_host=bootstrap.bind_host,
            public_ports=(bootstrap.daemon_port, bootstrap.websocket_port),
            bootstrap_dir=bootstrap_dir,
        )

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    def start(self) -> None:
        """Wait for the public pair, spawn gdaemon, and wait until it serves both ports."""
        self._wait_for_public_ports_free()
        self._spawn()
        try:
            self._wait_until_serving()
        except FrontDoorStartupError:
            # A live child that never bound would read as serving to the monitor.
            proc = self._proc
            if proc is not None:
                _terminate(proc)
            raise

    def arm_respawn(self) -> None:
        """Respawn a crashed child with backoff until shutdown disarms it."""
        if self._disarmed or self._monitor is not None:
            return
        self._monitor = asyncio.get_running_loop().create_task(
            self._watch(), name="gdaemon-front-door-monitor"
        )

    def disarm(self) -> None:
        """Cancel the restart monitor and any pending backoff; safe from any thread."""
        with self._lock:
            self._disarmed = True
        monitor = self._monitor
        if monitor is not None and not monitor.done():
            monitor.get_loop().call_soon_threadsafe(monitor.cancel)

    def stop(self) -> None:
        """Disarm respawn, then SIGTERM, wait, SIGKILL, and close the liveness pipe."""
        self.disarm()
        with self._lock:
            proc, self._proc = self._proc, None
            write_fd, self._write_fd = self._write_fd, None
        try:
            if proc is not None:
                _terminate(proc)
        finally:
            if write_fd is not None:
                os.close(write_fd)

    def _wait_for_public_ports_free(self) -> None:
        # A previous child may still be draining after its parent pipe closed.
        deadline = time.monotonic() + PORT_REUSE_WAIT_SECONDS
        for port in self._public_ports:
            while not _bindable(self._bind_host, port):
                if time.monotonic() >= deadline:
                    raise FrontDoorStartupError(
                        f"Port {port} on {self._bind_host} is still in use after "
                        f"{PORT_REUSE_WAIT_SECONDS:.0f}s; another process holds the public port"
                    )
                time.sleep(POLL_INTERVAL_SECONDS)

    def _spawn(self) -> None:
        with self._lock:
            if self._disarmed:
                raise FrontDoorStartupError("gdaemon front door is shutting down")
            if self._write_fd is not None:
                os.close(self._write_fd)
                self._write_fd = None
            self._proc, self._write_fd = self._popen()
            logger.info("Started gdaemon front door (PID %s)", self._proc.pid)

    def _popen(self) -> tuple[subprocess.Popen[bytes], int]:
        read_fd, write_fd = os.pipe()
        env = os.environ.copy()
        env["GOBBY_HOME"] = str(self._bootstrap_dir)
        env["GOBBY_FRONT_DOOR_SECRET"] = self.secret
        command = [self._binary, "serve"]
        try:
            if sys.platform == "win32":
                import msvcrt

                handle = msvcrt.get_osfhandle(read_fd)
                os.set_handle_inheritable(handle, True)
                env[PARENT_FD_ENV] = str(handle)
                proc = subprocess.Popen(  # nosec B603 # resolved gdaemon path, fixed argv
                    command,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    startupinfo=subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]}),
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                env[PARENT_FD_ENV] = str(read_fd)
                # A new session keeps a terminal's Ctrl-C away from the child; the
                # runner owns its lifecycle and the pipe covers the runner's death.
                proc = subprocess.Popen(  # nosec B603 # resolved gdaemon path, fixed argv
                    command,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(read_fd,),
                    start_new_session=True,
                )
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        return proc, write_fd

    def _wait_until_serving(self) -> None:
        proc = self._proc
        if proc is None:
            raise FrontDoorStartupError("gdaemon front door was stopped during startup")
        host = "localhost" if self._bind_host in _WILDCARD_HOSTS else self._bind_host
        http_port, ws_port = self._public_ports
        deadline = time.monotonic() + STARTUP_WINDOW_SECONDS
        while True:
            returncode = proc.poll()
            if returncode is not None:
                raise FrontDoorStartupError(
                    f"gdaemon serve exited with code {returncode} before binding the "
                    f"public ports {http_port}/{ws_port}"
                )
            if _accepts(host, http_port) and _accepts(host, ws_port):
                return
            if time.monotonic() >= deadline:
                raise FrontDoorStartupError(
                    f"gdaemon serve did not bind the public ports {http_port}/{ws_port} "
                    f"on {self._bind_host} within {STARTUP_WINDOW_SECONDS:.0f}s"
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    async def _watch(self) -> None:
        backoff = RESPAWN_INITIAL_BACKOFF_SECONDS
        # None while no child serves: failed respawns never reset the schedule.
        serving_since: float | None = time.monotonic()
        while not self._disarmed:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            proc = self._proc
            if proc is None or proc.poll() is None:
                continue
            if (
                serving_since is not None
                and time.monotonic() - serving_since >= RESPAWN_MAX_BACKOFF_SECONDS
            ):
                backoff = RESPAWN_INITIAL_BACKOFF_SECONDS
            serving_since = None
            logger.warning(
                "gdaemon front door (PID %s) exited with code %s; respawning in %.0fs",
                proc.pid,
                proc.returncode,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RESPAWN_MAX_BACKOFF_SECONDS)
            try:
                await asyncio.to_thread(self.start)
            except (FrontDoorStartupError, OSError) as exc:
                # OSError covers a transient os.pipe or Popen failure (EAGAIN, EMFILE);
                # the monitor is the only respawner, so it must outlive both.
                logger.error("gdaemon front door respawn failed: %s", exc)
                continue
            serving_since = time.monotonic()


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        logger.warning(
            "gdaemon front door (PID %s) ignored SIGTERM for %.0fs; killing",
            proc.pid,
            STOP_GRACE_SECONDS,
        )
    proc.kill()
    proc.wait()
