"""Tmux implementation of TerminalRuntime (plan 2.2)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.text_injection import (
    TMUX_TEXT_ENTER_DELAY_SECONDS,
    TmuxTargetUnavailableError,
    TmuxTextInjectionError,
    TmuxTextInjectionTimeout,
    paste_literal_text_to_tmux_target,
    send_enter_key_to_tmux_target,
    send_named_key_to_tmux_target,
)
from gobby.storage.terminals import (
    AttachLocator,
    Terminal,
)
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.host_protocol import frames_socket_path
from gobby.terminals.key_bytes import TMUX_KEY_NAMES, encode_named_key
from gobby.terminals.runtime import (
    MAX_INPUT_PAYLOAD_BYTES,
    MAX_RAW_INPUT_PAYLOAD_BYTES,
    CommitSpawnRefusedError,
    Delivered,
    IndeterminateWrite,
    InputPayloadTooLargeError,
    NamedKey,
    PreparedSpawn,
    SnapshotMode,
    SnapshotResult,
    TerminalHandle,
    TerminalSpawnFailed,
    TerminalSpawnRequest,
    TerminalWriteError,
    WriteOutcome,
)

__all__ = [
    "CommitSpawnRefusedError",
    "InputPayloadTooLargeError",
    "TmuxTerminalRuntime",
]

_GENERATION_FORMAT = "#{pid}\t#{start_time}"
# What a pane from another server generation reads as: nothing.
_EMPTY_SNAPSHOT = SnapshotResult(text="", truncated=False, dropped_bytes=0, total_bytes=0)


class TmuxTerminalRuntime:
    """Delegating tmux backend; does not write terminal rows."""

    backend: Literal["tmux", "native"] = "tmux"

    def __init__(
        self,
        *,
        host_control: Any | None = None,
        sessions_for_socket: Callable[[str], TmuxSessionManager] = TmuxSessionManager,
    ) -> None:
        self._host_control = host_control
        self._sessions_for_socket = sessions_for_socket

    def _sessions_for(self, terminal: Terminal) -> TmuxSessionManager:
        """The manager for the user's own server that recorded this pane.

        Gobby owns no tmux server, so a row that is not an external pane with a
        recorded socket has nothing to address.
        """
        locator = terminal.locator or {}
        socket_path = locator.get("socket_path")
        if terminal.ownership != "external" or not isinstance(socket_path, str) or not socket_path:
            raise TmuxTargetUnavailableError(
                "tmux row has no external socket to address", command=()
            )
        return self._sessions_for_socket(socket_path)

    def _cmd_for(self, terminal: Terminal) -> list[str]:
        return self._sessions_for(terminal).base_args()

    def _tmux_name(self, terminal: Terminal) -> str:
        return terminal.session_name or terminal.spawn_key or ""

    def _target(self, terminal: Terminal) -> str:
        locator = terminal.locator or {}
        pane_id = locator.get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            return pane_id
        name = self._tmux_name(terminal)
        if name:
            return f"={name}:"
        raise TerminalWriteError(stage="none")

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        """Refuse: agent spawn is native-only; tmux rows are external panes."""
        raise TerminalSpawnFailed("tmux backend does not spawn terminals")

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        """Refuse: nothing was prepared on tmux."""
        raise TerminalSpawnFailed("tmux backend does not spawn terminals")

    async def _same_generation(self, terminal: Terminal, target: str) -> bool:
        """True only while ``target`` is on the tmux server that recorded this row.

        A restarted server reuses pane ids, so the socket and pane alone can
        name an unrelated pane.
        """
        locator = terminal.locator or {}
        recorded = (locator.get("server_pid"), locator.get("server_start_time"))
        if not all(isinstance(part, int) and not isinstance(part, bool) for part in recorded):
            return False
        try:
            rc, stdout, _stderr = await self._sessions_for(terminal)._run(
                "display-message", "-p", "-t", target, _GENERATION_FORMAT
            )
        except (TimeoutError, OSError, TmuxTargetUnavailableError):
            return False
        return rc == 0 and stdout.strip() == "{}\t{}".format(*recorded)

    async def _require_generation(
        self, terminal: Terminal, target: str, *, stage: Literal["none", "partial"]
    ) -> None:
        if not await self._same_generation(terminal, target):
            raise TerminalWriteError(stage=stage)

    async def is_live(self, terminal: Terminal) -> bool:
        locator = terminal.locator or {}
        pane_id = locator.get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            if not await self._same_generation(terminal, pane_id):
                return False
            rc, stdout, _stderr = await self._sessions_for(terminal)._run(
                "display-message", "-p", "-t", pane_id, "#{pane_dead}"
            )
            return rc == 0 and stdout.strip() != "1"
        return await self.session_present(terminal)

    async def session_present(self, terminal: Terminal) -> bool:
        """True when the tmux session still exists, including remain-on-exit dead panes."""
        name = self._tmux_name(terminal)
        if not name:
            return False
        try:
            sessions = self._sessions_for(terminal)
        except TmuxTargetUnavailableError:
            return False
        return await sessions.has_session(name)

    async def snapshot(
        self, terminal: Terminal, lines: int = 50, *, mode: SnapshotMode = "text"
    ) -> SnapshotResult:
        target = self._capture_name(terminal)
        if not await self._same_generation(terminal, target):
            return _EMPTY_SNAPSHOT
        text = await self._sessions_for(terminal).capture_pane(target, lines=lines, mode=mode)
        return await self._snapshot_result(terminal, text or "")

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        target = self._capture_name(terminal)
        if not await self._same_generation(terminal, target):
            return _EMPTY_SNAPSHOT
        text = await self._sessions_for(terminal).capture_full_pane(target)
        return await self._snapshot_result(terminal, text or "")

    def _capture_name(self, terminal: Terminal) -> str:
        locator = terminal.locator or {}
        pane_id = locator.get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            return pane_id
        return self._tmux_name(terminal)

    async def _snapshot_result(self, terminal: Terminal, text: str) -> SnapshotResult:
        size, limit = await self._history_bounds(terminal)
        if size is not None and limit is not None and size >= limit:
            return SnapshotResult(
                text=text,
                truncated=True,
                dropped_bytes=None,
                total_bytes=None,
            )
        encoded = text.encode("utf-8")
        return SnapshotResult(
            text=text,
            truncated=False,
            dropped_bytes=0,
            total_bytes=len(encoded),
        )

    async def _history_bounds(self, terminal: Terminal) -> tuple[int | None, int | None]:
        target = self._target(terminal)
        try:
            rc, stdout, _stderr = await self._sessions_for(terminal)._run(
                "display-message",
                "-t",
                target,
                "-p",
                "#{history_size}|#{history_limit}",
            )
        except TimeoutError:
            return None, None
        if rc != 0 or "|" not in stdout:
            return None, None
        size_raw, limit_raw = stdout.strip().split("|", 1)
        try:
            return int(size_raw), int(limit_raw)
        except ValueError:
            return None, None

    async def write_text(self, terminal: Terminal, text: str, submit: bool) -> WriteOutcome:
        target = self._target(terminal)
        await self._require_generation(terminal, target, stage="none")
        body = text.rstrip("\n")
        payload_landed = False
        try:
            if body:
                await paste_literal_text_to_tmux_target(
                    target,
                    body,
                    tmux_cmd=self._cmd_for(terminal),
                )
                payload_landed = True
            if submit:
                if body and TMUX_TEXT_ENTER_DELAY_SECONDS > 0:
                    await asyncio.sleep(TMUX_TEXT_ENTER_DELAY_SECONDS)
                    await self._require_generation(terminal, target, stage="partial")
                await send_enter_key_to_tmux_target(target, tmux_cmd=self._cmd_for(terminal))
            return Delivered()
        except TmuxTextInjectionTimeout:
            return IndeterminateWrite(detail="tmux send-keys timed out")
        except TimeoutError:
            return IndeterminateWrite(detail="tmux invocation timed out")
        except asyncio.CancelledError:
            raise
        except TmuxTextInjectionError as exc:
            raise TerminalWriteError(stage="partial" if payload_landed else "none") from exc

    async def write_key(self, terminal: Terminal, key: NamedKey) -> WriteOutcome:
        target = self._target(terminal)
        await self._require_generation(terminal, target, stage="none")
        try:
            cursor, keypad, _paste = await self._query_flags(terminal, target)
            named = TMUX_KEY_NAMES.get(key)
            if named is not None:
                await send_named_key_to_tmux_target(target, named, tmux_cmd=self._cmd_for(terminal))
                return Delivered()
            encoded = encode_named_key(key, cursor_app=cursor, keypad_app=keypad)
            hex_bytes = [f"{byte:02x}" for byte in encoded]
            await self._sessions_for(terminal)._run("send-keys", "-t", target, "-H", *hex_bytes)
            return Delivered()
        except TmuxTextInjectionTimeout:
            return IndeterminateWrite(detail="tmux send-keys timed out")
        except TimeoutError:
            return IndeterminateWrite(detail="tmux invocation timed out")
        except asyncio.CancelledError:
            raise
        except TmuxTextInjectionError as exc:
            raise TerminalWriteError(stage="none") from exc

    async def write_input(self, terminal: Terminal, data: bytes) -> WriteOutcome:
        if len(data) > MAX_RAW_INPUT_PAYLOAD_BYTES:
            raise InputPayloadTooLargeError("input exceeds 64 KiB")
        target = self._target(terminal)
        await self._require_generation(terminal, target, stage="none")
        delivered_bytes = 0
        for offset in range(0, len(data), 512):
            chunk = data[offset : offset + 512]
            try:
                rc, _stdout, _stderr = await self._sessions_for(terminal)._run(
                    "send-keys",
                    "-t",
                    target,
                    "-H",
                    *(f"{byte:02x}" for byte in chunk),
                )
            except asyncio.CancelledError:
                raise
            except (TmuxTextInjectionTimeout, TimeoutError) as exc:
                raise TerminalWriteError(stage="partial") from exc
            if rc != 0:
                stage: Literal["none", "partial"] = "partial" if delivered_bytes else "none"
                raise TerminalWriteError(
                    stage=stage,
                    delivered_bytes=delivered_bytes,
                )
            delivered_bytes += len(chunk)
        return Delivered()

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        if len(text.encode("utf-8")) > MAX_INPUT_PAYLOAD_BYTES:
            raise InputPayloadTooLargeError("paste exceeds 1 MiB UTF-8")
        target = self._target(terminal)
        await self._require_generation(terminal, target, stage="none")
        try:
            _cursor, _keypad, bracketed = await self._query_flags(terminal, target)
            payload = f"\x1b[200~{text}\x1b[201~" if bracketed else text
            await paste_literal_text_to_tmux_target(
                target, payload, tmux_cmd=self._cmd_for(terminal)
            )
            return Delivered()
        except TmuxTextInjectionTimeout:
            return IndeterminateWrite(detail="tmux send-keys timed out")
        except TimeoutError:
            return IndeterminateWrite(detail="tmux invocation timed out")
        except asyncio.CancelledError:
            raise
        except TmuxTextInjectionError as exc:
            raise TerminalWriteError(stage="none") from exc

    async def resize(self, terminal: Terminal, rows: int, cols: int) -> None:
        validate_dimensions(rows, cols)
        target = self._target(terminal)
        if not await self._same_generation(terminal, target):
            return
        sessions = self._sessions_for(terminal)
        await sessions._run("set-option", "-w", "-t", target, "window-size", "manual")
        await sessions._run(
            "resize-window",
            "-t",
            target,
            "-x",
            str(cols),
            "-y",
            str(rows),
        )

    async def release_size(self, terminal: Terminal) -> None:
        target = self._target(terminal)
        if await self._same_generation(terminal, target):
            await self._sessions_for(terminal)._run(
                "set-option", "-wu", "-t", target, "window-size"
            )

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        name = self._tmux_name(terminal)
        # A restarted server's same-named session belongs to someone else.
        if name and await self._same_generation(terminal, self._target(terminal)):
            # Kill on the terminal's own socket: presence checks use
            # _sessions_for, so killing on the default socket would no-op
            # for external rows and report the session as still present.
            await self._sessions_for(terminal).kill_session(name, timeout=grace_seconds)

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        locator = terminal.locator or {}
        pid = locator.get("server_pid")
        start = locator.get("server_start_time")
        manager = getattr(self._host_control, "_manager", None)
        directory = getattr(manager, "socket_dir", None)
        if directory is None:
            directory = getattr(self._host_control, "socket_dir", None)
        return AttachLocator(
            backend="tmux",
            frame_host_epoch=str(
                terminal.host_epoch or getattr(self._host_control, "host_epoch", "") or ""
            ),
            host_socket=(None if directory is None else str(frames_socket_path(Path(directory)))),
            host_terminal_id=(
                None if locator.get("pane_id") is None else str(locator.get("pane_id"))
            ),
            socket_path=None
            if locator.get("socket_path") is None
            else str(locator.get("socket_path")),
            pane_id=None if locator.get("pane_id") is None else str(locator.get("pane_id")),
            server_pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            server_start_time=(
                start if isinstance(start, int) and not isinstance(start, bool) else None
            ),
        )

    async def _query_flags(self, terminal: Terminal, target: str) -> tuple[bool, bool, bool]:
        rc, stdout, _stderr = await self._sessions_for(terminal)._run(
            "display-message",
            "-t",
            target,
            "-p",
            "#{cursor_keys_flag}|#{keypad_cursor_flag}|#{bracket_paste_flag}",
        )
        if rc != 0:
            return False, False, False
        parts = stdout.strip().split("|")
        while len(parts) < 3:
            parts.append("0")
        return parts[0] == "1", parts[1] == "1", parts[2] == "1"
