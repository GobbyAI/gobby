"""Tmux implementation of TerminalRuntime (plan 2.2)."""

from __future__ import annotations

import asyncio
from typing import Literal

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.spawner import tmux_spawn_shell_and_env, validate_spawn_key
from gobby.agents.tmux.text_injection import (
    TMUX_TEXT_ENTER_DELAY_SECONDS,
    TmuxTextInjectionError,
    TmuxTextInjectionTimeout,
    paste_literal_text_to_tmux_target,
    send_enter_key_to_tmux_target,
    send_named_key_to_tmux_target,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.terminals import (
    AttachLocator,
    Terminal,
    parse_tmux_generation,
    tmux_locator_key,
)
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.runtime import (
    MAX_INPUT_PAYLOAD_BYTES,
    CommitSpawnRefusedError,
    Delivered,
    IndeterminateWrite,
    InputPayloadTooLargeError,
    NamedKey,
    PreparedSpawn,
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

_NAMED_TMUX_KEYS: dict[str, str] = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
}
_CURSOR_LETTERS: dict[str, str] = {"up": "A", "down": "B", "right": "C", "left": "D"}
_KEYPAD_APP: dict[str, bytes] = {
    "kp0": b"\x1bOp",
    "kp1": b"\x1bOq",
    "kp2": b"\x1bOr",
    "kp3": b"\x1bOs",
    "kp4": b"\x1bOt",
    "kp5": b"\x1bOu",
    "kp6": b"\x1bOv",
    "kp7": b"\x1bOw",
    "kp8": b"\x1bOx",
    "kp9": b"\x1bOy",
    "kpdecimal": b"\x1bOn",
    "kpminus": b"\x1bOm",
    "kpplus": b"\x1bOk",
    "kpmul": b"\x1bOj",
    "kpdiv": b"\x1bOo",
    "kpenter": b"\x1bOM",
}
_KEYPAD_NORMAL: dict[str, bytes] = {
    "kp0": b"0",
    "kp1": b"1",
    "kp2": b"2",
    "kp3": b"3",
    "kp4": b"4",
    "kp5": b"5",
    "kp6": b"6",
    "kp7": b"7",
    "kp8": b"8",
    "kp9": b"9",
    "kpdecimal": b".",
    "kpminus": b"-",
    "kpplus": b"+",
    "kpmul": b"*",
    "kpdiv": b"/",
    "kpenter": b"\r",
}


class TmuxTerminalRuntime:
    """Delegating tmux backend; does not write terminal rows."""

    backend: Literal["tmux", "native"] = "tmux"

    def __init__(
        self,
        sessions: TmuxSessionManager,
        *,
        frame_host_epoch: str = "",
    ) -> None:
        self._sessions = sessions
        self._frame_host_epoch = frame_host_epoch

    def _cmd(self) -> list[str]:
        return self._sessions.base_args()

    def _sessions_for(self, terminal: Terminal) -> TmuxSessionManager:
        locator = terminal.locator or {}
        socket_path = locator.get("socket_path")
        if terminal.ownership == "external" and isinstance(socket_path, str) and socket_path:
            return TmuxSessionManager(TmuxConfig(socket_name="", socket_path=socket_path))
        return self._sessions

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

    async def _run(self, *args: str) -> tuple[int, str, str]:
        return await self._sessions._run(*args)

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        if request.rows is not None and request.cols is not None:
            validate_dimensions(request.rows, request.cols)
        elif request.rows is not None or request.cols is not None:
            validate_dimensions(
                request.rows if request.rows is not None else 0,
                request.cols if request.cols is not None else 0,
            )
        spawn_key = validate_spawn_key(request.spawn_key)
        shell_cmd, extra_env = tmux_spawn_shell_and_env(
            request.command,
            request.env,
            request.auth_cli,
        )
        try:
            info = await self._sessions.create_session(
                spawn_key,
                command=shell_cmd,
                cwd=request.cwd,
                env=extra_env,
            )
        except Exception as exc:
            raise TerminalSpawnFailed(str(exc)) from exc
        rc, stdout, _stderr = await self._run(
            "display-message",
            "-t",
            f"={info.name}:",
            "-p",
            "#{socket_path}|#{pid}|#{start_time}|#{pane_id}",
        )
        locator: AttachLocator | None = None
        stored_locator: dict[str, object] | None = None
        locator_key: str | None = None
        host_terminal_id: str | None = info.pane_id
        if rc == 0 and stdout.strip():
            parsed = parse_tmux_generation(stdout.strip())
            pane_id = str(parsed["pane_id"])
            socket_path = str(parsed["socket_path"])
            raw_pid = parsed["server_pid"]
            raw_start = parsed["server_start_time"]
            if not isinstance(raw_pid, int) or not isinstance(raw_start, int):
                raise TerminalSpawnFailed("tmux generation fields were not integers")
            server_pid = raw_pid
            server_start_time = raw_start
            locator = AttachLocator(
                backend="tmux",
                frame_host_epoch=self._frame_host_epoch,
                socket_path=socket_path,
                pane_id=pane_id,
            )
            stored_locator = {
                "socket_path": socket_path,
                "server_pid": server_pid,
                "server_start_time": server_start_time,
                "pane_id": pane_id,
            }
            locator_key = tmux_locator_key(
                socket_path=socket_path,
                server_pid=server_pid,
                server_start_time=server_start_time,
                pane_id=pane_id,
            )
            host_terminal_id = pane_id
        return PreparedSpawn(
            terminal_id=request.terminal_id,
            spawn_key=spawn_key,
            locator=locator,
            process=None,
            host_terminal_id=host_terminal_id,
            stored_locator=stored_locator,
            locator_key=locator_key,
        )

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        if not prepared.persist_acknowledged:
            raise CommitSpawnRefusedError("persist has not been acknowledged")
        locator = prepared.locator or AttachLocator(
            backend="tmux",
            frame_host_epoch=self._frame_host_epoch,
        )
        return TerminalHandle(terminal_id=prepared.terminal_id, locator=locator)

    async def is_live(self, terminal: Terminal) -> bool:
        locator = terminal.locator or {}
        pane_id = locator.get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            rc, stdout, _stderr = await self._sessions_for(terminal)._run(
                "display-message", "-p", "-t", pane_id, "#{pane_dead}"
            )
            return rc == 0 and stdout.strip() != "1"
        name = self._tmux_name(terminal)
        if not name:
            return False
        return await self._sessions_for(terminal).has_session(name)

    async def snapshot(self, terminal: Terminal, lines: int = 50) -> SnapshotResult:
        text = await self._sessions_for(terminal).capture_pane(
            self._capture_name(terminal), lines=lines
        )
        return await self._snapshot_result(terminal, text or "")

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        text = await self._sessions_for(terminal).capture_full_pane(self._capture_name(terminal))
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
        try:
            cursor, keypad, _paste = await self._query_flags(terminal, target)
            named = _NAMED_TMUX_KEYS.get(key)
            if named is not None:
                await send_named_key_to_tmux_target(target, named, tmux_cmd=self._cmd_for(terminal))
                return Delivered()
            encoded = _encode_key(key, cursor_app=cursor, keypad_app=keypad)
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

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        if len(text.encode("utf-8")) > MAX_INPUT_PAYLOAD_BYTES:
            raise InputPayloadTooLargeError("paste exceeds 1 MiB UTF-8")
        target = self._target(terminal)
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
        await self._run(
            "resize-window",
            "-t",
            self._target(terminal),
            "-x",
            str(cols),
            "-y",
            str(rows),
        )

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        name = self._tmux_name(terminal)
        if name:
            await self._sessions.kill_session(name, timeout=grace_seconds)

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        locator = terminal.locator or {}
        pid = locator.get("server_pid")
        start = locator.get("server_start_time")
        return AttachLocator(
            backend="tmux",
            frame_host_epoch=self._frame_host_epoch or str(terminal.host_epoch or ""),
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


def _encode_key(key: NamedKey, *, cursor_app: bool, keypad_app: bool) -> bytes:
    letter = _CURSOR_LETTERS.get(key)
    if letter is not None:
        prefix = b"\x1bO" if cursor_app else b"\x1b["
        return prefix + letter.encode("ascii")
    table = _KEYPAD_APP if keypad_app else _KEYPAD_NORMAL
    encoded = table.get(key)
    if encoded is None:
        raise TerminalWriteError(stage="none")
    return encoded
