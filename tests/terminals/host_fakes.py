"""Injectable doubles for TerminalHostManager tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4


@dataclass
class FakePing:
    host_epoch: str
    host_pid: int
    version: str = "0.1.0"


@dataclass
class FakeHello:
    host_epoch: str
    version: str = "0.1.0"
    protocol_version: int = 1


@dataclass
class FakeListRow:
    terminal_id: str
    spawn_key: str
    commit_state: Literal["prepared", "committed"] = "committed"
    observer_bind: Literal["reserved", "none"] = "none"
    host_terminal_id: str = "ht-1"
    pgid: int | None = None
    start_time: float | None = None


@dataclass
class FakeControlClient:
    """In-memory control client used by host-manager tests."""

    host_epoch: str = field(default_factory=lambda: str(uuid4()))
    host_pid: int = 4242
    version: str = "0.1.0"
    protocol_version: int = 1
    token: str = "control-token"
    terminals: list[FakeListRow] = field(default_factory=list)
    hello_error: str | None = None
    closed: bool = False
    draining: bool = False
    shutdown_calls: list[int] = field(default_factory=list)
    spawn_commits: list[tuple[str, str]] = field(default_factory=list)
    kill_calls: list[str] = field(default_factory=list)
    record_process_hook: Any = None
    drop_on_commit: bool = False
    claimed: bool = False

    async def hello(self, protocol_version: int, control_token: str) -> FakeHello:
        if self.hello_error is not None:
            raise PermissionError(self.hello_error)
        if self.token and control_token != self.token and self.token != "control-token":
            raise PermissionError("invalid_token")
        if protocol_version != self.protocol_version:
            raise PermissionError("unsupported_protocol")
        return FakeHello(
            host_epoch=self.host_epoch,
            version=self.version,
            protocol_version=self.protocol_version,
        )

    async def ping(self) -> FakePing:
        self._require_open()
        return FakePing(
            host_epoch=self.host_epoch,
            host_pid=self.host_pid,
            version=self.version,
        )

    async def list_terminals(self) -> list[FakeListRow]:
        self._require_open()
        self.claimed = True
        return list(self.terminals)

    async def host_shutdown(self, grace_ms: int) -> dict[str, bool]:
        self._require_open()
        self.shutdown_calls.append(grace_ms)
        self.draining = True
        return {"accepted": True, "draining": True}

    async def spawn_commit(self, terminal_id: str, spawn_key: str) -> None:
        self._require_open()
        if self.drop_on_commit:
            self.closed = True
            raise ConnectionError("control dropped")
        self.spawn_commits.append((terminal_id, spawn_key))

    async def kill(self, host_terminal_id: str) -> None:
        self._require_open()
        self.kill_calls.append(host_terminal_id)

    async def close(self) -> None:
        # Tests reuse one client across adopt/replace/stop; do not latch closed.
        return None

    def _require_open(self) -> None:
        if self.closed:
            raise ConnectionError("control closed")


@dataclass
class FakeHostProcess:
    pid: int
    running: bool = True

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.running = False


class FakeRunManager:
    def __init__(self) -> None:
        self.interrupted: list[str] = []

    def cancel(self, run_id: str, *, terminal_reason: str | None = None) -> object:
        self.interrupted.append(run_id)
        assert terminal_reason == "daemon_stop"
        return object()
