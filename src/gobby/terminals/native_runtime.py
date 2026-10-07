"""Native TerminalRuntime over the gterm control client (plan 4.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gobby.agents.constants import GOBBY_TERMINAL_ID
from gobby.agents.tmux.text_injection import TMUX_TEXT_ENTER_DELAY_SECONDS
from gobby.storage.terminals import (
    AttachLocator,
    Terminal,
    native_attach_locator,
    native_locator_key,
)
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import (
    MAX_CONTROL_LINE,
    MAX_WRITE_BATCH_TARGETS,
    CommitTransportError,
    HostBatchOperation,
    HostBatchTarget,
    HostCommandError,
    HostDecodeError,
    HostEpochChangedError,
    HostManagerStopped,
    HostNotAdoptedError,
    HostUnavailableError,
    encode_control_line,
)
from gobby.terminals.host_protocol import control_socket_path, frames_socket_path
from gobby.terminals.host_reap import reap_recorded_process, recorded_process_group_is_alive
from gobby.terminals.host_reconcile import reconcile_host_inventory
from gobby.terminals.key_bytes import encode_named_key
from gobby.terminals.native_env_policy import apply_native_env_policy
from gobby.terminals.native_frames import NativeFrameStreamMixin, _mark_host_error_stage
from gobby.terminals.native_host_probe import NativeHostProbeMixin
from gobby.terminals.runtime import (
    MAX_INPUT_PAYLOAD_BYTES,
    MAX_RAW_INPUT_PAYLOAD_BYTES,
    CommitSpawnRefusedError,
    Delivered,
    IndeterminateWrite,
    InputPayloadTooLargeError,
    NamedKey,
    PreparedSpawn,
    ProcessIdentity,
    SnapshotMode,
    SnapshotResult,
    TerminalHandle,
    TerminalSpawnRequest,
    TerminalWriteError,
    WriteOutcome,
    is_named_key,
)
from gobby.terminals.termination import TerminalKillUnprovenError

MAX_SPAWN_ERROR_CODE_LENGTH = 128
# The host escalates to SIGKILL after grace; allow for its delivery and reap.
GROUP_EXIT_MARGIN_SECONDS = 2.0
GROUP_EXIT_POLL_SECONDS = 0.05
type NativeSpawnSettlement = Literal["fail_pending", "fail_pending_kill", "pending"]


@dataclass(frozen=True, slots=True)
class HostEpochMismatch:
    """A captured host resource belongs to an earlier host incarnation."""

    expected_epoch: str | None
    current_epoch: str | None


def _usable_process_group(process: Mapping[str, Any]) -> bool:
    # recorded_process_group_is_alive answers false for a missing or invalid
    # pgid, so only a positive integer pgid makes its false mean death.
    pgid = process.get("pgid")
    return isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 0


async def _await_group_exit(terminal: Terminal, grace_seconds: float) -> None:
    """Prove a killed terminal's recorded group died before its row may settle.

    gterm 0.1.3 acks a kill only after its group is gone, but an older host
    acks once SIGTERM is sent, so a group that ignores SIGTERM can outlive
    that ack. The daemon checks the recorded group itself either way.
    """
    process = terminal.process
    if process is None or not _usable_process_group(process):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_seconds + GROUP_EXIT_MARGIN_SECONDS
    while await asyncio.to_thread(recorded_process_group_is_alive, process):
        if loop.time() >= deadline:
            raise TerminalKillUnprovenError(
                f"Terminal {terminal.id} process group is alive after the host kill"
            )
        await asyncio.sleep(GROUP_EXIT_POLL_SECONDS)


def _bounded_spawn_code(code: str) -> str:
    return (code or "spawn_failed")[:MAX_SPAWN_ERROR_CODE_LENGTH]


def _host_error_detail(exc: HostCommandError) -> str | None:
    if exc.detail and exc.stage:
        return f"{exc.stage}: {exc.detail}"
    if exc.detail:
        return exc.detail
    if exc.stage:
        return f"stage: {exc.stage}"
    return None


def classify_native_spawn_failure(
    exc: BaseException,
) -> tuple[str, str | None, NativeSpawnSettlement]:
    """Map every native spawn failure to one bounded code and row settlement."""
    if isinstance(exc, HostManagerStopped):
        return "host_stopped", exc.detail, "fail_pending"
    if isinstance(exc, HostEpochChangedError):
        return "host_epoch_changed", str(exc), "fail_pending"
    if isinstance(exc, HostNotAdoptedError):
        return "host_not_adopted", exc.detail, "fail_pending"
    if isinstance(exc, CommitTransportError):
        if exc.request_written:
            return "commit_indeterminate", exc.detail, "pending"
        return "commit_not_sent", exc.detail, "fail_pending_kill"
    if isinstance(exc, asyncio.CancelledError):
        if bool(getattr(exc, "request_written", False)):
            return "commit_indeterminate", None, "pending"
        return "commit_not_sent", None, "fail_pending_kill"
    if isinstance(exc, HostUnavailableError):
        return "host_unreachable", exc.detail, "fail_pending"
    if isinstance(exc, HostCommandError):
        detail = _host_error_detail(exc)
        if exc.stage == "reserve" and exc.error in {
            "host_draining",
            "capacity",
            "stale",
            "stale_reservation",
            "not_native",
        }:
            reason = "stale" if exc.error == "stale_reservation" else exc.error
            return _bounded_spawn_code(f"host_refused:{reason}"), detail, "fail_pending"
        if exc.error == "exec_failed":
            return _bounded_spawn_code(f"exec_failed:{exc.code}"), detail, "fail_pending"
        if exc.error in {"exec_timeout", "malformed_status"}:
            return exc.error, detail, "fail_pending"
        if exc.error in {"unknown_terminal", "already_committed"}:
            return _bounded_spawn_code(f"commit_refused:{exc.error}"), detail, "fail_pending"
        return _bounded_spawn_code(exc.error), detail, "fail_pending"
    if isinstance(exc, CommitSpawnRefusedError):
        return "commit_refused:invalid_state", str(exc), "fail_pending"
    return _bounded_spawn_code(str(exc)), str(exc) or None, "fail_pending"


@dataclass(frozen=True)
class NativeBatchOperation:
    kind: Literal["text", "key"]
    payload: str
    delay_ms: int = 0


@dataclass(frozen=True)
class NativeBatchTarget:
    result_id: str
    terminal: Terminal
    operations: tuple[NativeBatchOperation, ...]


@dataclass(frozen=True)
class NativeBatchFailure:
    stage: Literal["none", "partial"]
    code: str
    detail: str


@dataclass(frozen=True)
class NativeBatchResult:
    result_id: str
    outcome: WriteOutcome | NativeBatchFailure


class HostManagerControl:
    """Delegates control verbs to the supervisor's connected client."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self.attaches: list[str | None] = []

    @property
    def host_epoch(self) -> str | None:
        return getattr(self._manager, "host_epoch", None)

    @property
    def commit_deadline_ms(self) -> int:
        config = getattr(self._manager, "config", None)
        return int(getattr(config, "commit_deadline_ms", 30_000))

    @property
    def closed(self) -> bool:
        client = getattr(self._manager, "_client", None)
        return client is None or bool(getattr(client, "closed", False))

    async def ensure_connected(self) -> None:
        if getattr(self._manager, "_client", None) is None:
            raise HostUnavailableError("gterm host unavailable")

    def __getattr__(self, name: str) -> Any:
        client = getattr(self._manager, "_client", None)
        if client is None:
            raise HostUnavailableError("gterm host unavailable")
        return getattr(client, name)


__all__ = [
    "HostManagerControl",
    "NativeBatchFailure",
    "NativeBatchOperation",
    "NativeBatchResult",
    "NativeBatchTarget",
    "NativeTerminalRuntime",
    "classify_native_spawn_failure",
]


class NativeTerminalRuntime(NativeFrameStreamMixin, NativeHostProbeMixin):
    """Control-client backend; does not write terminal rows."""

    backend: Literal["tmux", "native"] = "native"

    def __init__(
        self,
        client: Any,
        *,
        frame_host_epoch: str = "",
        terminal_manager: Any | None = None,
        machine_id: str = "",
        spawn_in_doubt_seconds: float = 30.0,
        frame_client: Any | None = None,
        run_manager: Any | None = None,
    ) -> None:
        self._client = client
        del frame_host_epoch
        self._terminal_manager = terminal_manager
        self._machine_id = machine_id
        self._spawn_in_doubt_seconds = spawn_in_doubt_seconds
        self._frame_client = frame_client
        # Self-opened observer streams keyed by host terminal id. The host
        # replaces a stream's attachment on every attach and closes the stream
        # once its terminal is removed, so each bound terminal needs its own.
        self._observer_streams: dict[str, FrameClient] = {}
        self._run_manager = run_manager
        self._subscribed = False

    @classmethod
    def preflight_line(cls, payload: dict[str, Any]) -> bytes:
        encoded = encode_control_line(payload)
        if len(encoded) >= MAX_CONTROL_LINE:
            raise HostCommandError("request_too_large")
        return encoded

    async def _ensure(self) -> None:
        ensure = getattr(self._client, "ensure_connected", None)
        if callable(ensure):
            await ensure()
            return
        if getattr(self._client, "closed", False):
            raise HostUnavailableError("gterm host unavailable")

    def _host_id(self, terminal: Terminal) -> str:
        locator = terminal.locator or terminal.process or {}
        host_id = locator.get("host_terminal_id")
        if isinstance(host_id, str) and host_id:
            return host_id
        raise TerminalWriteError(stage="none")

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        try:
            await self._ensure()
            if request.rows is not None and request.cols is not None:
                validate_dimensions(request.rows, request.cols)
            reservation_id = request.reservation_id
            reserve_key = request.reserve_key
            if not reservation_id or not reserve_key:
                raise HostCommandError("invalid_reservation")
            argv, env = apply_native_env_policy(request.command, request.env, request.auth_cli)
            # A pane id names a pane only within its host epoch.
            seed: dict[str, str] = {}
            if request.theme_from is not None:
                seed_epoch, seed_pane = request.theme_from
                if seed_epoch == str(getattr(self._client, "host_epoch", "") or ""):
                    seed["theme_from"] = seed_pane
            payload = await self._client.spawn(
                # None: the child starts on theme_from's ground, else unset.
                terminal_theme=request.terminal_theme,
                **seed,
                terminal_id=str(request.terminal_id),
                spawn_key=request.spawn_key,
                reservation_id=reservation_id,
                reserve_key=reserve_key,
                argv=argv,
                # After the caller env so nothing shadows the minted identity.
                env={**env, GOBBY_TERMINAL_ID: str(request.terminal_id)},
                cwd=request.cwd,
                rows=request.rows or 24,
                cols=request.cols or 80,
            )
        except HostCommandError as exc:
            _mark_host_error_stage(exc, "prepare")
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise HostUnavailableError(str(exc) or "gterm host unavailable") from exc
        host_terminal_id = str(payload.get("host_terminal_id") or "")
        pgid = payload.get("pgid")
        start_time = payload.get("start_time")
        process = None
        if isinstance(pgid, int):
            process = ProcessIdentity(pgid=pgid, start_time=int(float(start_time or 0)))
        epoch = str(getattr(self._client, "host_epoch", "") or "")
        locator = AttachLocator(
            backend="native",
            frame_host_epoch=epoch,
            host_terminal_id=host_terminal_id,
        )
        return PreparedSpawn(
            terminal_id=request.terminal_id,
            spawn_key=request.spawn_key,
            locator=locator,
            process=process,
            host_terminal_id=host_terminal_id,
            stored_locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key(epoch, host_terminal_id),
        )

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        if not prepared.persist_acknowledged or not prepared.observer_bound:
            raise CommitSpawnRefusedError("persist and observer bind have not been acknowledged")
        locator = prepared.locator or AttachLocator(
            backend="native",
            frame_host_epoch=str(getattr(self._client, "host_epoch", "") or ""),
            host_terminal_id=prepared.host_terminal_id,
        )
        expected_epoch = locator.frame_host_epoch
        current_epoch = str(getattr(self._client, "host_epoch", "") or "")
        if expected_epoch and current_epoch and current_epoch != expected_epoch:
            raise HostEpochChangedError("host epoch changed")
        try:
            commit_deadline_ms = int(getattr(self._client, "commit_deadline_ms", 30_000))
            await self._client.spawn_commit(
                str(prepared.terminal_id), prepared.spawn_key, commit_deadline_ms
            )
        except CommitTransportError:
            raise
        except HostCommandError as exc:
            _mark_host_error_stage(exc, "commit")
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise CommitTransportError(str(exc), request_written=False) from exc
        return TerminalHandle(terminal_id=prepared.terminal_id, locator=locator)

    async def is_live(self, terminal: Terminal) -> bool:
        if not terminal.host_epoch:
            return False
        try:
            await self._ensure()
            rows = await self._client.list_terminals()
        except (HostUnavailableError, ConnectionError, OSError):
            return False
        epoch = terminal.host_epoch
        if str(getattr(self._client, "host_epoch", "") or "") != epoch:
            return False
        host_id = (terminal.locator or {}).get("host_terminal_id")
        for row in rows:
            if str(row.terminal_id) == terminal.id and str(row.spawn_key) == str(
                terminal.spawn_key
            ):
                return True
            if host_id and str(row.host_terminal_id) == str(host_id):
                return True
        return False

    async def session_present(self, terminal: Terminal) -> bool:
        # Native terminals have no remain-on-exit: presence is liveness.
        return await self.is_live(terminal)

    async def snapshot(
        self, terminal: Terminal, lines: int = 50, *, mode: SnapshotMode = "text"
    ) -> SnapshotResult:
        return await self._snapshot(terminal, max_lines=lines, mode=mode)

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        return await self._snapshot(terminal, max_lines=10_000, mode="text")

    async def _snapshot(
        self, terminal: Terminal, max_lines: int, mode: SnapshotMode
    ) -> SnapshotResult:
        await self._ensure()
        try:
            payload = await self._client.snapshot(
                self._host_id(terminal),
                mode=mode,
                max_lines=max_lines,
            )
        except HostCommandError as exc:
            if exc.error == "not_found":
                return SnapshotResult(text="", truncated=False, dropped_bytes=0, total_bytes=0)
            raise
        text = str(payload.get("text", ""))
        truncated = bool(payload.get("truncated", False))
        dropped = payload.get("dropped_bytes")
        total = payload.get("total_bytes")
        return SnapshotResult(
            text=text,
            truncated=truncated,
            dropped_bytes=int(dropped) if isinstance(dropped, int) else 0,
            total_bytes=int(total) if isinstance(total, int) else len(text.encode("utf-8")),
        )

    async def write_batch(self, targets: Sequence[NativeBatchTarget]) -> list[NativeBatchResult]:
        """Dispatch one bounded host request and preserve a result for every target."""
        if len(targets) > MAX_WRITE_BATCH_TARGETS:
            failure = NativeBatchFailure(
                stage="none",
                code="too_many_targets",
                detail=f"native wake batches are limited to {MAX_WRITE_BATCH_TARGETS} targets",
            )
            return [NativeBatchResult(target.result_id, failure) for target in targets]

        results: dict[str, NativeBatchResult] = {}
        host_targets: list[HostBatchTarget] = []
        try:
            await self._ensure()
        except HostUnavailableError as exc:
            failure = NativeBatchFailure(stage="none", code=exc.code, detail=exc.message)
            return [NativeBatchResult(target.result_id, failure) for target in targets]

        for target in targets:
            try:
                host_terminal_id = self._host_id(target.terminal)
            except TerminalWriteError as exc:
                results[target.result_id] = NativeBatchResult(
                    target.result_id,
                    NativeBatchFailure(
                        stage=exc.stage,
                        code="host_terminal_id_missing",
                        detail=str(exc),
                    ),
                )
                continue
            operations: list[HostBatchOperation] = []
            invalid_key = False
            for operation in target.operations:
                if operation.kind == "key":
                    if not is_named_key(operation.payload):
                        invalid_key = True
                        break
                    data = encode_named_key(operation.payload)
                else:
                    data = operation.payload.encode("utf-8")
                operations.append(
                    HostBatchOperation(
                        kind=operation.kind,
                        data=data,
                        delay_ms=operation.delay_ms,
                    )
                )
            if invalid_key:
                results[target.result_id] = NativeBatchResult(
                    target.result_id,
                    NativeBatchFailure(
                        stage="none",
                        code="invalid_key",
                        detail="native wake batch contains an invalid named key",
                    ),
                )
                continue
            host_targets.append(
                HostBatchTarget(
                    recipient_id=target.result_id,
                    host_terminal_id=host_terminal_id,
                    operations=tuple(operations),
                )
            )

        if host_targets:
            try:
                host_results = await self._client.write_batch(host_targets)
            except HostCommandError as exc:
                failure = NativeBatchFailure(stage="none", code=exc.code, detail=str(exc))
                for host_target in host_targets:
                    results[host_target.recipient_id] = NativeBatchResult(
                        host_target.recipient_id, failure
                    )
            except (ConnectionError, HostDecodeError) as exc:
                for host_target in host_targets:
                    results[host_target.recipient_id] = NativeBatchResult(
                        host_target.recipient_id,
                        IndeterminateWrite(detail=str(exc)),
                    )
            else:
                for item in host_results:
                    result_id = str(item["recipient_id"])
                    if item.get("ok") is True:
                        outcome: WriteOutcome | NativeBatchFailure = Delivered()
                    else:
                        stage: Literal["none", "partial"] = (
                            "partial" if item.get("stage") == "partial" else "none"
                        )
                        code = str(item.get("error") or "terminal_write_failed")
                        outcome = NativeBatchFailure(stage=stage, code=code, detail=code)
                    results[result_id] = NativeBatchResult(result_id, outcome)

        return [results[target.result_id] for target in targets]

    async def write_text(
        self,
        terminal: Terminal,
        text: str,
        submit: bool,
        operation_seq: int | None = None,
    ) -> WriteOutcome:
        return await self._write(
            terminal,
            kind="text",
            data=text.encode("utf-8"),
            submit=submit,
            operation_seq=operation_seq,
        )

    async def write_key(self, terminal: Terminal, key: NamedKey) -> WriteOutcome:
        # gterm passes unknown key names through as literal bytes, so encode here.
        return await self._write(terminal, kind="key", data=encode_named_key(key))

    async def write_input(self, terminal: Terminal, data: bytes) -> WriteOutcome:
        if len(data) > MAX_RAW_INPUT_PAYLOAD_BYTES:
            raise InputPayloadTooLargeError("input exceeds 64 KiB")
        return await self._write(terminal, kind="input", data=data)

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_INPUT_PAYLOAD_BYTES:
            raise InputPayloadTooLargeError("paste exceeds 1 MiB UTF-8")
        return await self._write(terminal, kind="paste", data=encoded)

    async def _write(
        self,
        terminal: Terminal,
        *,
        kind: str,
        data: bytes,
        submit: bool = False,
        operation_seq: int | None = None,
    ) -> WriteOutcome:
        try:
            await self._ensure()
            host_id = self._host_id(terminal)
        except HostUnavailableError as exc:
            raise TerminalWriteError(stage="none") from exc
        if kind == "text" and submit and operation_seq is None:
            try:
                await self._client.write(
                    host_terminal_id=host_id,
                    kind="text",
                    data=data,
                    submit=False,
                )
            except HostUnavailableError as exc:
                raise TerminalWriteError(stage="none") from exc
            except HostCommandError as exc:
                raise TerminalWriteError(stage="none") from exc
            except ConnectionError as exc:
                return IndeterminateWrite(detail=str(exc))
            if TMUX_TEXT_ENTER_DELAY_SECONDS > 0:
                await asyncio.sleep(TMUX_TEXT_ENTER_DELAY_SECONDS)
            try:
                await self._client.write(
                    host_terminal_id=host_id,
                    kind="key",
                    data=b"enter",
                )
            except HostCommandError as exc:
                raise TerminalWriteError(stage="partial") from exc
            except ConnectionError as exc:
                return IndeterminateWrite(detail=str(exc))
            return Delivered()
        try:
            await self._client.write(
                host_terminal_id=host_id,
                kind=kind,
                data=data,
                submit=submit,
                operation_seq=operation_seq,
            )
        except HostUnavailableError as exc:
            raise TerminalWriteError(stage="none") from exc
        except HostCommandError:
            raise
        except ConnectionError as exc:
            return IndeterminateWrite(detail=str(exc))
        return Delivered()

    async def resize(self, terminal: Terminal, rows: int, cols: int) -> None:
        validate_dimensions(rows, cols)
        expected_epoch = self._require_current_epoch(terminal.host_epoch)
        try:
            await self._ensure()
            await self._client.resize(self._host_id(terminal), rows, cols)
        except (HostUnavailableError, ConnectionError, OSError):
            await self._reconnect_epoch(expected_epoch)
            await self._client.resize(self._host_id(terminal), rows, cols)

    async def grant_input(self, terminal: Terminal, attachment_id: str) -> None:
        """Hand the host's frame-stream input grant for ``terminal`` to ``attachment_id``."""
        expected_epoch = self._require_current_epoch(terminal.host_epoch)
        host_id = self._host_id(terminal)
        try:
            await self._ensure()
            await self._client.grant_input(host_id, attachment_id)
        except (HostUnavailableError, ConnectionError, OSError):
            await self._reconnect_epoch(expected_epoch)
            await self._client.grant_input(host_id, attachment_id)

    async def revoke_input(self, terminal: Terminal, attachment_id: str | None = None) -> None:
        """Clear the host's input grant for ``terminal``; unnamed, whoever holds it."""
        expected_epoch = self._require_current_epoch(terminal.host_epoch)
        host_id = self._host_id(terminal)
        try:
            await self._ensure()
            await self._client.revoke_input(host_id, attachment_id)
        except (HostUnavailableError, ConnectionError, OSError):
            await self._reconnect_epoch(expected_epoch)
            await self._client.revoke_input(host_id, attachment_id)

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        grace_ms = max(0, int(grace_seconds * 1000)) or 50
        # Connect before the epoch compare: an unconnected client after a
        # restart has no epoch yet and would make a current row look stale.
        connect_error: Exception | None = None
        try:
            await self._ensure()
        except (HostUnavailableError, ConnectionError, OSError) as exc:
            connect_error = exc
        current_epoch = str(getattr(self._client, "host_epoch", "") or "")
        if not terminal.host_epoch or terminal.host_epoch != current_epoch:
            await self._terminate_stale(terminal, grace_seconds, grace_ms, connect_error)
            return
        expected_epoch = self._require_current_epoch(terminal.host_epoch)
        host_terminal_id = self._host_id(terminal)
        try:
            await self._ensure()
            await self._client.kill(host_terminal_id, grace_ms=grace_ms)
        except (HostUnavailableError, ConnectionError, OSError):
            await self._reconnect_epoch(expected_epoch)
            await self._client.kill(host_terminal_id, grace_ms=grace_ms)
        await _await_group_exit(terminal, grace_seconds)

    async def _terminate_stale(
        self,
        terminal: Terminal,
        grace_seconds: float,
        grace_ms: int,
        connect_error: Exception | None,
    ) -> None:
        # A row without the current epoch (stale, missing, or pending) returns
        # only on proof: a kill through the host id the current host lists, or
        # a strict listing without it (#22530: reap the recorded process), each
        # followed by a usable recorded group verified dead; or, with the host
        # unreachable, that group verified dead. Anything else raises.
        if connect_error is not None:
            await self._reap_proven_dead(terminal, grace_seconds, connect_error)
            return
        try:
            host_terminal_id = await self.find_host_terminal(
                terminal.id, str(terminal.spawn_key or terminal.id)
            )
        except (HostUnavailableError, ConnectionError, OSError) as exc:
            await self._reap_proven_dead(terminal, grace_seconds, exc)
            return
        if host_terminal_id is not None:
            await self._client.kill(host_terminal_id, grace_ms=grace_ms)
        elif terminal.process:
            await asyncio.to_thread(
                reap_recorded_process, terminal.process, grace_seconds=grace_seconds
            )
        await _await_group_exit(terminal, grace_seconds)

    async def _reap_proven_dead(
        self, terminal: Terminal, grace_seconds: float, host_error: Exception
    ) -> None:
        # With the host unable to answer, only a usable recorded process group
        # (a positive integer pgid) verified dead after the reap is proof.
        # recorded_process_group_is_alive also answers false for a missing or
        # invalid pgid, so that false is never read as death.
        process = terminal.process
        if process is not None and _usable_process_group(process):
            await asyncio.to_thread(reap_recorded_process, process, grace_seconds=grace_seconds)
            if not recorded_process_group_is_alive(process):
                return
        raise host_error

    async def kill(self, host_terminal_id: str, grace_seconds: float = 0.05) -> None:
        """Kill one resource on the currently connected host."""
        grace_ms = max(0, int(grace_seconds * 1000))
        try:
            await self._ensure()
            await self._client.kill(host_terminal_id, grace_ms=grace_ms or 50)
        except (HostUnavailableError, ConnectionError, OSError, TimeoutError):
            await self.reconnect()
            await self._client.kill(host_terminal_id, grace_ms=grace_ms or 50)

    async def terminate_host_id(
        self,
        host_terminal_id: str,
        host_epoch: str | None,
        grace_seconds: float = 0.05,
    ) -> HostEpochMismatch | None:
        """Kill a captured host id only while its captured epoch is still current."""
        await self._ensure()
        current_epoch = getattr(self._client, "host_epoch", None)
        normalized_epoch = None if current_epoch is None else str(current_epoch)
        if host_epoch != normalized_epoch:
            return HostEpochMismatch(host_epoch, normalized_epoch)
        grace_ms = max(0, int(grace_seconds * 1000))
        await self._client.kill(host_terminal_id, grace_ms=grace_ms or 50)
        return None

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        directory = self._socket_dir()
        live = str(getattr(self._client, "host_epoch", "") or "") or str(terminal.host_epoch or "")
        return native_attach_locator(
            terminal,
            live_host_epoch=live,
            host_socket=None if directory is None else str(frames_socket_path(directory)),
        )

    async def reconnect(self) -> str:
        reconnect = getattr(self._client, "reconnect", None)
        if callable(reconnect):
            current_epoch = str(getattr(self._client, "host_epoch", "") or "")
            directory = self._socket_dir()
            if directory is None:
                raise HostUnavailableError("gterm host socket unavailable")
            epoch = await reconnect(control_socket_path(directory), current_epoch or None)
        else:
            epoch = getattr(self._client, "host_epoch", "")
        normalized_epoch = str(epoch)
        if self._terminal_manager is not None:
            rows = await self._client.list_terminals()
            await reconcile_host_inventory(
                terminal_manager=self._terminal_manager,
                machine_id=self._machine_id,
                host_epoch=normalized_epoch,
                host_rows=rows,
                spawn_in_doubt_seconds=self._spawn_in_doubt_seconds,
                run_manager=self._run_manager,
                kill=self.kill,
            )
        return normalized_epoch

    def _require_current_epoch(self, expected_epoch: str | None) -> str:
        if not expected_epoch:
            raise HostEpochChangedError("host_epoch_changed")
        current_epoch = str(getattr(self._client, "host_epoch", "") or "")
        if not current_epoch:
            raise HostNotAdoptedError()
        if current_epoch != expected_epoch:
            raise HostEpochChangedError("host_epoch_changed")
        return expected_epoch

    async def _reconnect_epoch(self, expected_epoch: str) -> None:
        directory = self._socket_dir()
        reconnect = getattr(self._client, "reconnect", None)
        if directory is None or not callable(reconnect):
            raise HostUnavailableError("gterm host socket unavailable")
        await reconnect(control_socket_path(directory), expected_epoch)
