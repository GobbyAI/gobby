"""Backend-neutral TerminalRuntime contract (plan 2.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeIs, get_args
from uuid import UUID

from gobby.storage.terminals import AttachLocator, Terminal

NamedKey = Literal[
    "enter",
    "escape",
    "tab",
    "up",
    "down",
    "left",
    "right",
    "kp0",
    "kp1",
    "kp2",
    "kp3",
    "kp4",
    "kp5",
    "kp6",
    "kp7",
    "kp8",
    "kp9",
    "kpdecimal",
    "kpplus",
    "kpminus",
    "kpmul",
    "kpdiv",
    "kpenter",
]

MAX_INPUT_PAYLOAD_BYTES = 1024 * 1024


def is_named_key(value: str) -> TypeIs[NamedKey]:
    """Narrow a write payload to the NamedKey contract."""
    return value in get_args(NamedKey)


@dataclass(frozen=True)
class ProcessIdentity:
    """Native process identity recorded after prepare_spawn."""

    pgid: int
    start_time: int


@dataclass
class PreparedSpawn:
    """Staged handoff between backend effect and caller acknowledgements."""

    terminal_id: UUID
    spawn_key: str
    locator: AttachLocator | None
    process: ProcessIdentity | None
    host_terminal_id: str | None
    persist_acknowledged: bool = False
    observer_bound: bool = False
    stored_locator: dict[str, object] | None = None
    locator_key: str | None = None
    pid: int | None = None

    def acknowledge_persist(self) -> None:
        self.persist_acknowledged = True

    def acknowledge_observer(self) -> None:
        self.observer_bound = True


@dataclass(frozen=True)
class TerminalHandle:
    """Committed terminal identity plus the backend locator."""

    terminal_id: UUID
    locator: AttachLocator


@dataclass
class TerminalSpawnRequest:
    """Caller-allocated spawn identity plus command payload."""

    terminal_id: UUID
    spawn_key: str
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    rows: int | None = None
    cols: int | None = None
    title: str | None = None
    labels: dict[str, str] | None = None
    reservation_id: str | None = None
    reserve_key: str | None = None
    auth_cli: str | None = None


@dataclass(frozen=True)
class SnapshotResult:
    """Captured text plus UTF-8 byte counters; None means the backend cannot know."""

    text: str
    truncated: bool
    dropped_bytes: int | None
    total_bytes: int | None


@dataclass(frozen=True)
class Delivered:
    """Write reached the backend and a definitive success reply arrived."""


class IndeterminateWrite(Exception):
    """Backend effect may have landed; the reply was lost or timed out."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Suppressed:
    """Automatic write skipped because this action_key is already unresolved."""

    action_key: str
    reason: str = "unresolved_write"


@dataclass(frozen=True)
class AutomaticWriteQuarantined:
    """Automatic write refused because the terminal is quarantined."""

    action_key: str
    reason: str = "automatic_write_quarantined"


@dataclass(frozen=True)
class LoopMisuse:
    """Sync bridge called on the runner loop thread; nothing was dispatched."""

    code: Literal["loop_misuse"] = "loop_misuse"


WriteOutcome = Delivered | IndeterminateWrite | Suppressed | AutomaticWriteQuarantined


class UnregisteredBackendError(KeyError):
    """Raised when the runtime registry has no implementation for a backend."""

    def __init__(self, backend: str) -> None:
        super().__init__(backend)
        self.backend = backend


class CommitSpawnRefusedError(RuntimeError):
    """commit_spawn called before the caller acknowledged persist."""


class TerminalSpawnFailed(RuntimeError):
    """Backend spawn failed with a definitive, no-effect outcome."""


class InvalidSpawnKeyError(ValueError):
    """Caller-supplied spawn_key would need rewriting to be a backend name."""


class InputPayloadTooLargeError(ValueError):
    """Write payload exceeded MAX_INPUT_PAYLOAD_BYTES."""


class TerminalWriteError(RuntimeError):
    """Typed write failure that did not lose its injection stage."""

    def __init__(self, *, stage: Literal["none", "partial"]) -> None:
        super().__init__(f"terminal write failed at stage {stage}")
        self.stage = stage


class TerminalRuntime(Protocol):
    """Lifecycle, snapshot, input, resize, and attach-locator resolution.

    Continuous output streaming is deliberately outside this contract.
    """

    backend: Literal["tmux", "native"]

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn: ...

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle: ...

    async def is_live(self, terminal: Terminal) -> bool: ...

    async def snapshot(self, terminal: Terminal, lines: int = 50) -> SnapshotResult: ...

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult: ...

    async def write_text(self, terminal: Terminal, text: str, submit: bool) -> WriteOutcome: ...

    async def write_key(self, terminal: Terminal, key: NamedKey) -> WriteOutcome: ...

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome: ...

    async def resize(self, terminal: Terminal, rows: int, cols: int) -> None: ...

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None: ...

    async def attach_locator(self, terminal: Terminal) -> AttachLocator: ...


class ObserverReservingRuntime(Protocol):
    """Native runtimes that hold an observer slot before prepare_spawn."""

    async def reserve_observer(self, terminal_id: UUID) -> Mapping[str, str]: ...


def can_reserve_observer(runtime: object) -> TypeIs[ObserverReservingRuntime]:
    """Narrow a runtime that implements native observer reservation."""
    reserve = getattr(runtime, "reserve_observer", None)
    return callable(reserve)
