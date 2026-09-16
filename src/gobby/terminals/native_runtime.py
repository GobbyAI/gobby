"""Native TerminalRuntime over the gterm control client (plan 4.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from gobby.storage.terminals import AttachLocator, Terminal, native_locator_key
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
from gobby.terminals.host_protocol import HostListRow, control_socket_path, frames_socket_path
from gobby.terminals.host_reconcile import reconcile_host_inventory
from gobby.terminals.key_bytes import encode_named_key
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
    SnapshotResult,
    TerminalHandle,
    TerminalSpawnRequest,
    TerminalWriteError,
    WriteOutcome,
    is_named_key,
)
from gobby.utils.local_token import LOCAL_API_TOKEN_FILENAME, local_token_path

MAX_SPAWN_ERROR_CODE_LENGTH = 128
type NativeSpawnSettlement = Literal["fail_pending", "fail_pending_kill", "pending"]


@dataclass(frozen=True, slots=True)
class HostEpochMismatch:
    """A captured host resource belongs to an earlier host incarnation."""

    expected_epoch: str | None
    current_epoch: str | None


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


def _mark_host_error_stage(exc: HostCommandError, stage: str) -> None:
    if exc.stage is None:
        exc.stage = stage


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


class NativeTerminalRuntime:
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

    def _socket_dir(self) -> Path | None:
        manager = getattr(self._client, "_manager", None)
        directory = getattr(manager, "socket_dir", None)
        if directory is None:
            directory = getattr(self._client, "socket_dir", None)
        if directory is None:
            return None
        return Path(directory)

    def _frame_token(self) -> str:
        # gterm resolves the same credential from its socket directory first and
        # falls back to Gobby home, so a host whose socket directory carries no
        # token file still authenticates. Resolving only the socket directory
        # here sends an empty token and the host answers `invalid_token`.
        candidates: list[Path] = []
        directory = self._socket_dir()
        if directory is not None:
            candidates.append(directory / LOCAL_API_TOKEN_FILENAME)
        candidates.append(local_token_path())
        for path in candidates:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token:
                return token
        return ""

    async def _open_frame_stream(self, locator: AttachLocator) -> FrameClient:
        epoch = locator.frame_host_epoch or str(getattr(self._client, "host_epoch", "") or "")
        directory = self._socket_dir()
        if directory is None:
            raise HostCommandError("attach_failed")
        try:
            reader, writer = await asyncio.open_unix_connection(str(frames_socket_path(directory)))
        except (OSError, ConnectionError) as exc:
            raise HostCommandError("attach_failed") from exc
        client = FrameClient(reader, writer)
        try:
            await client.handshake(
                AttachLocator(
                    backend="native",
                    frame_host_epoch=epoch,
                    host_terminal_id=locator.host_terminal_id,
                ),
                local_token=self._frame_token(),
            )
        except BaseException:
            await client.close()
            raise
        return client

    async def _bind_frames(self, locator: AttachLocator, reservation_id: str) -> None:
        """Attach the daemon observer for one terminal on a stream of its own.

        A frame stream belongs to the host that answered its handshake and to
        the terminal it last attached: the host swaps the attachment on every
        ``attach_terminal`` and closes the stream when that terminal is removed
        or its unread frames lag out. Reusing one stream across terminals
        therefore unbinds the previous observer and, after the first terminal
        exits, writes every later bind into a dead socket. Each bind gets a
        fresh stream, drained by a pump that forgets it at EOF.
        """
        if self._frame_client is not None:
            await self._frame_client.attach_terminal(locator, reservation_id=reservation_id)
            return
        key = locator.host_terminal_id or ""
        stream = await self._open_frame_stream(locator)
        try:
            await stream.attach_terminal(locator, reservation_id=reservation_id)
        except BaseException:
            await stream.close()
            raise
        previous = self._observer_streams.pop(key, None)
        self._observer_streams[key] = stream
        stream.start_pump(on_closed=lambda: self._forget_stream(key, stream))
        if previous is not None:
            await previous.close()

    def _forget_stream(self, key: str, stream: FrameClient) -> None:
        if self._observer_streams.get(key) is stream:
            del self._observer_streams[key]

    async def close_frame_streams(self) -> None:
        streams = list(self._observer_streams.values())
        self._observer_streams.clear()
        for stream in streams:
            await stream.close()

    async def reserve_observer(self, terminal_id: UUID) -> Mapping[str, str]:
        try:
            await self._ensure()
            subscribe = getattr(self._client, "subscribe_events", None)
            if callable(subscribe) and not self._subscribed:
                await subscribe()
                self._subscribed = True
            reserve_key = str(terminal_id)
            payload = await self._client.reserve_observer(str(terminal_id), reserve_key)
        except HostCommandError as exc:
            _mark_host_error_stage(exc, "reserve")
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise HostUnavailableError(str(exc) or "gterm host unavailable") from exc
        return {
            "reservation_id": str(payload.get("reservation_id") or ""),
            "reserve_key": str(payload.get("reserve_key") or reserve_key),
        }

    async def release_observer(self, reservation_id: str, reserve_key: str) -> Mapping[str, Any]:
        await self._ensure()
        release = getattr(self._client, "release_observer", None)
        if not callable(release):
            return {"ok": True, "released": True}
        payload = await release(reservation_id, reserve_key)
        return payload if isinstance(payload, dict) else {"ok": True, "released": True}

    async def bind_observer(self, prepared: PreparedSpawn, reservation_id: str) -> None:
        locator = prepared.locator or AttachLocator(
            backend="native",
            frame_host_epoch=str(getattr(self._client, "host_epoch", "") or ""),
            host_terminal_id=prepared.host_terminal_id,
        )
        await self._bind_frames(locator, reservation_id)
        prepared.acknowledge_observer()

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        try:
            await self._ensure()
            if request.rows is not None and request.cols is not None:
                validate_dimensions(request.rows, request.cols)
            reservation_id = request.reservation_id
            reserve_key = request.reserve_key
            if not reservation_id or not reserve_key:
                raise HostCommandError("invalid_reservation")
            payload = await self._client.spawn(
                terminal_id=str(request.terminal_id),
                spawn_key=request.spawn_key,
                reservation_id=reservation_id,
                reserve_key=reserve_key,
                argv=list(request.command),
                env=dict(request.env or {}),
                cwd=request.cwd or "/tmp",
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

    async def snapshot(self, terminal: Terminal, lines: int = 50) -> SnapshotResult:
        return await self._snapshot(terminal, max_lines=lines)

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        return await self._snapshot(terminal, max_lines=10_000)

    async def _snapshot(self, terminal: Terminal, max_lines: int) -> SnapshotResult:
        await self._ensure()
        try:
            payload = await self._client.snapshot(
                self._host_id(terminal),
                mode="text",
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

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        expected_epoch = self._require_current_epoch(terminal.host_epoch)
        host_terminal_id = self._host_id(terminal)
        grace_ms = max(0, int(grace_seconds * 1000)) or 50
        try:
            await self._ensure()
            await self._client.kill(host_terminal_id, grace_ms=grace_ms)
        except (HostUnavailableError, ConnectionError, OSError):
            await self._reconnect_epoch(expected_epoch)
            await self._client.kill(host_terminal_id, grace_ms=grace_ms)

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
        locator = terminal.locator or {}
        host_id = locator.get("host_terminal_id")
        directory = self._socket_dir()
        return AttachLocator(
            backend="native",
            frame_host_epoch=str(
                terminal.host_epoch or getattr(self._client, "host_epoch", "") or ""
            ),
            host_socket=None if directory is None else str(frames_socket_path(directory)),
            host_terminal_id=None if host_id is None else str(host_id),
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

    async def rebind_prepared(
        self,
        prepared: PreparedSpawn,
        reservation_id: str | None = None,
    ) -> None:
        rows: list[HostListRow] = await self._client.list_terminals()
        match = next(
            (
                row
                for row in rows
                if str(row.terminal_id) == str(prepared.terminal_id)
                and str(row.spawn_key) == prepared.spawn_key
            ),
            None,
        )
        if match is None:
            raise HostCommandError("not_found")
        if match.observer_bind == "none":
            raise HostCommandError("observer_bind_none")
        rid = reservation_id
        attaches = getattr(self._client, "attaches", None)
        if isinstance(attaches, list):
            attaches.append(rid)
        if rid is not None:
            locator = AttachLocator(
                backend="native",
                frame_host_epoch=str(getattr(self._client, "host_epoch", "") or ""),
                host_terminal_id=match.host_terminal_id,
            )
            await self._bind_frames(locator, rid)

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
