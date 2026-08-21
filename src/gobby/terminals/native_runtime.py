"""Native TerminalRuntime over the gterm control client (plan 4.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from gobby.storage.terminals import AttachLocator, Terminal, native_locator_key
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import (
    MAX_CONTROL_LINE,
    HostCommandError,
    HostUnavailableError,
    encode_control_line,
)
from gobby.terminals.host_protocol import HostListRow, control_socket_path, frames_socket_path
from gobby.terminals.host_reconcile import reconcile_host_inventory
from gobby.terminals.runtime import (
    MAX_INPUT_PAYLOAD_BYTES,
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
)


class HostManagerControl:
    """Delegates control verbs to the supervisor's connected client."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self.attaches: list[str | None] = []

    @property
    def host_epoch(self) -> str | None:
        return getattr(self._manager, "host_epoch", None)

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


__all__ = ["HostManagerControl", "NativeTerminalRuntime"]


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
        self._frame_host_epoch = frame_host_epoch
        self._terminal_manager = terminal_manager
        self._machine_id = machine_id
        self._spawn_in_doubt_seconds = spawn_in_doubt_seconds
        self._frame_client = frame_client
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
        locator = terminal.locator or {}
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
        directory = self._socket_dir()
        if directory is None:
            return ""
        path = directory / "local_cli_token"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def _ensure_frame_client(self, locator: AttachLocator) -> Any:
        existing = self._frame_client
        if existing is not None and not bool(getattr(existing, "closed", False)):
            return existing
        directory = self._socket_dir()
        if directory is None:
            raise HostCommandError("attach_failed")
        try:
            reader, writer = await asyncio.open_unix_connection(str(frames_socket_path(directory)))
        except (OSError, ConnectionError) as exc:
            raise HostCommandError("attach_failed") from exc
        client = FrameClient(reader, writer)
        epoch = locator.frame_host_epoch or self._frame_host_epoch
        if not epoch:
            epoch = str(getattr(self._client, "host_epoch", "") or "")
        await client.handshake(
            AttachLocator(
                backend="native",
                frame_host_epoch=epoch,
                host_terminal_id=locator.host_terminal_id,
            ),
            local_token=self._frame_token(),
        )
        self._frame_client = client
        self._frame_host_epoch = epoch
        return client

    async def reserve_observer(self, terminal_id: UUID) -> Mapping[str, str]:
        await self._ensure()
        subscribe = getattr(self._client, "subscribe_events", None)
        if callable(subscribe) and not self._subscribed:
            await subscribe()
            self._subscribed = True
        reserve_key = str(terminal_id)
        payload = await self._client.reserve_observer(str(terminal_id), reserve_key)
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
            frame_host_epoch=self._frame_host_epoch,
            host_terminal_id=prepared.host_terminal_id,
        )
        client = await self._ensure_frame_client(locator)
        await client.attach_terminal(locator, reservation_id=reservation_id)
        prepared.acknowledge_observer()

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
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
        host_terminal_id = str(payload.get("host_terminal_id") or "")
        pgid = payload.get("pgid")
        start_time = payload.get("start_time")
        process = None
        if isinstance(pgid, int):
            process = ProcessIdentity(pgid=pgid, start_time=int(float(start_time or 0)))
        epoch = self._frame_host_epoch or str(getattr(self._client, "host_epoch", "") or "")
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
            frame_host_epoch=self._frame_host_epoch,
            host_terminal_id=prepared.host_terminal_id,
        )
        try:
            await self._client.spawn_commit(str(prepared.terminal_id), prepared.spawn_key)
        except ConnectionError:
            directory = self._socket_dir()
            reconnect = getattr(self._client, "reconnect", None)
            if directory is None or not callable(reconnect):
                raise
            await reconnect(
                control_socket_path(directory), expected_epoch=self._frame_host_epoch or None
            )
            rows = await self._client.list_terminals()
            match = next(
                (
                    row
                    for row in rows
                    if str(row.terminal_id) == str(prepared.terminal_id)
                    and str(row.spawn_key) == prepared.spawn_key
                    and row.commit_state == "committed"
                ),
                None,
            )
            if match is None:
                raise
        return TerminalHandle(terminal_id=prepared.terminal_id, locator=locator)

    async def is_live(self, terminal: Terminal) -> bool:
        try:
            await self._ensure()
            rows = await self._client.list_terminals()
        except (HostUnavailableError, ConnectionError, OSError):
            return False
        epoch = terminal.host_epoch or self._frame_host_epoch
        host_id = (terminal.locator or {}).get("host_terminal_id")
        for row in rows:
            if str(row.terminal_id) == terminal.id and str(row.spawn_key) == str(
                terminal.spawn_key
            ):
                if epoch and getattr(self._client, "host_epoch", epoch) not in {None, epoch}:
                    return False
                return True
            if host_id and str(row.host_terminal_id) == str(host_id):
                return True
        return False

    async def snapshot(self, terminal: Terminal, lines: int = 50) -> SnapshotResult:
        return await self._snapshot(terminal, max_lines=lines)

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        return await self._snapshot(terminal, max_lines=10_000)

    async def _snapshot(self, terminal: Terminal, max_lines: int) -> SnapshotResult:
        await self._ensure()
        payload = await self._client.snapshot(
            self._host_id(terminal),
            mode="text",
            max_lines=max_lines,
        )
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
        return await self._write(terminal, kind="key", data=key.encode("utf-8"))

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
        try:
            await self._ensure()
            await self._client.resize(self._host_id(terminal), rows, cols)
        except ConnectionError:
            reconnect = getattr(self._client, "reconnect", None)
            if callable(reconnect):
                await reconnect()
                await self._client.resize(self._host_id(terminal), rows, cols)

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        grace_ms = max(0, int(grace_seconds * 1000))
        try:
            await self._ensure()
            await self._client.kill(self._host_id(terminal), grace_ms=grace_ms or 50)
        except ConnectionError:
            reconnect = getattr(self._client, "reconnect", None)
            if callable(reconnect):
                await reconnect()
                await self._client.kill(self._host_id(terminal), grace_ms=grace_ms or 50)

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        locator = terminal.locator or {}
        host_id = locator.get("host_terminal_id")
        return AttachLocator(
            backend="native",
            frame_host_epoch=str(terminal.host_epoch or self._frame_host_epoch),
            host_terminal_id=None if host_id is None else str(host_id),
        )

    async def reconnect(self) -> str:
        reconnect = getattr(self._client, "reconnect", None)
        if callable(reconnect):
            epoch = await reconnect()
        else:
            epoch = getattr(self._client, "host_epoch", self._frame_host_epoch)
        self._frame_host_epoch = str(epoch)
        if self._terminal_manager is not None:

            async def kill(host_terminal_id: str) -> None:
                await self._client.kill(host_terminal_id)

            rows = await self._client.list_terminals()
            await reconcile_host_inventory(
                terminal_manager=self._terminal_manager,
                machine_id=self._machine_id,
                host_epoch=self._frame_host_epoch,
                host_rows=rows,
                spawn_in_doubt_seconds=self._spawn_in_doubt_seconds,
                run_manager=self._run_manager,
                kill=kill,
            )
        return self._frame_host_epoch

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
        if self._frame_client is not None and rid is not None:
            locator = AttachLocator(
                backend="native",
                frame_host_epoch=self._frame_host_epoch,
                host_terminal_id=match.host_terminal_id,
            )
            await self._frame_client.attach_terminal(locator, reservation_id=rid)
