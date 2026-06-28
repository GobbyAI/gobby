"""ACP terminal lifecycle support."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gobby.agents.constants import UV_CACHE_DIR

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_BYTE_LIMIT = 20_000
MAX_OUTPUT_BYTE_LIMIT = 16 * 1024 * 1024
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", UV_CACHE_DIR)


class TerminalNotFoundError(ValueError):
    """Raised when a terminal ID is unknown or already released."""


def _output_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_OUTPUT_BYTE_LIMIT
    if isinstance(value, bool):
        raise ValueError("terminal/create outputByteLimit must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("terminal/create outputByteLimit must be an integer") from exc
    return min(max(parsed, 0), MAX_OUTPUT_BYTE_LIMIT)


def _args(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("terminal/create args must be an array")
    return [str(item) for item in value]


def _env(value: Any) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    if value is None:
        pass
    elif isinstance(value, Mapping):
        env.update({str(key): str(val) for key, val in value.items()})
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("terminal/create env entries must be objects")
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("terminal/create env entries require a name")
            env[name] = str(item.get("value", ""))
    else:
        raise ValueError("terminal/create env must be an array")

    env["GOBBY_HOOKS_DISABLED"] = "1"
    env["GOBBY_ACP_CHILD_TOOL"] = "1"
    return env


def _cwd(value: Any, default_cwd: str | None) -> str:
    raw = value if value is not None else default_cwd
    path = Path(str(raw)).expanduser() if raw else Path.cwd()
    if value is not None and not path.is_absolute():
        raise ValueError("terminal/create cwd must be an absolute path")
    return str(path.resolve())


def _trim_output(data: bytes, limit: int) -> tuple[bytes, bool]:
    if len(data) <= limit:
        return data, False
    if limit <= 0:
        return b"", True
    tail = data[-limit:]
    while tail and tail[0] & 0b1100_0000 == 0b1000_0000:
        tail = tail[1:]
    return tail, True


def _decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="ignore")


def _exit_status(returncode: int | None) -> dict[str, int | str | None] | None:
    if returncode is None:
        return None
    if returncode < 0:
        signal_number = -returncode
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"SIG{signal_number}"
        return {"exitCode": None, "signal": signal_name}
    return {"exitCode": returncode, "signal": None}


@dataclass
class _Terminal:
    process: asyncio.subprocess.Process
    output_limit: int
    output: bytes = b""
    truncated: bool = False
    reader_task: asyncio.Task[None] | None = field(default=None)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def append(self, chunk: bytes) -> None:
        async with self.lock:
            self.output, truncated = _trim_output(self.output + chunk, self.output_limit)
            self.truncated = self.truncated or truncated

    async def snapshot(self) -> tuple[str, bool]:
        async with self.lock:
            return _decode_output(self.output), self.truncated


class ACPTerminalManager:
    """Manages ACP terminal IDs and subprocess lifecycle."""

    def __init__(self) -> None:
        self._terminals: dict[str, _Terminal] = {}

    async def create(
        self, params: Mapping[str, Any], *, default_cwd: str | None = None
    ) -> dict[str, str]:
        command = str(params.get("command") or "")
        if not command:
            raise ValueError("terminal/create command is required")

        output_limit = _output_limit(params.get("outputByteLimit"))
        proc = await asyncio.create_subprocess_exec(
            command,
            *_args(params.get("args")),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=_cwd(params.get("cwd"), default_cwd),
            env=_env(params.get("env")),
        )
        terminal_id = f"term_{uuid.uuid4().hex}"
        terminal = _Terminal(process=proc, output_limit=output_limit)
        terminal.reader_task = asyncio.create_task(self._read_output(terminal_id, terminal))
        self._terminals[terminal_id] = terminal
        return {"terminalId": terminal_id}

    async def output(self, terminal_id: str) -> dict[str, Any]:
        terminal = self._get(terminal_id)
        output, truncated = await terminal.snapshot()
        return {
            "output": output,
            "truncated": truncated,
            "exitStatus": _exit_status(terminal.process.returncode),
        }

    async def wait_for_exit(self, terminal_id: str) -> dict[str, int | str | None]:
        terminal = self._get(terminal_id)
        await terminal.process.wait()
        await self._wait_for_reader(terminal)
        status = _exit_status(terminal.process.returncode)
        return status or {"exitCode": None, "signal": None}

    async def kill(self, terminal_id: str) -> dict[str, Any]:
        terminal = self._get(terminal_id)
        if terminal.process.returncode is None:
            terminal.process.kill()
            await terminal.process.wait()
        await self._wait_for_reader(terminal)
        return {}

    async def release(self, terminal_id: str) -> dict[str, Any]:
        terminal = self._get(terminal_id)
        if terminal.process.returncode is None:
            terminal.process.kill()
            await terminal.process.wait()
        await self._wait_for_reader(terminal)
        self._terminals.pop(terminal_id, None)
        return {}

    async def release_all(self) -> None:
        for terminal_id in list(self._terminals):
            try:
                await self.release(terminal_id)
            except Exception:
                logger.debug("Failed to release ACP terminal %s", terminal_id, exc_info=True)

    def _get(self, terminal_id: str) -> _Terminal:
        terminal = self._terminals.get(terminal_id)
        if terminal is None:
            raise TerminalNotFoundError(f"Unknown terminalId: {terminal_id}")
        return terminal

    async def _read_output(self, terminal_id: str, terminal: _Terminal) -> None:
        stream = terminal.process.stdout
        if stream is None:
            return
        try:
            while chunk := await stream.read(8192):
                await terminal.append(chunk)
        except Exception:
            logger.debug("ACP terminal reader failed for %s", terminal_id, exc_info=True)

    async def _wait_for_reader(self, terminal: _Terminal) -> None:
        if terminal.reader_task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(terminal.reader_task), timeout=1.0)
        except TimeoutError:
            terminal.reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await terminal.reader_task
