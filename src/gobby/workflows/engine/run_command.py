"""Bounded subprocess execution for workflow ``run_command`` effects."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from gobby.hooks._normalization_tools import normalize_tool_fields
from gobby.hooks.events import HookEvent, HookEventType

STDOUT_LIMIT_BYTES = 256 * 1024
STDERR_LIMIT_BYTES = 64 * 1024

RunCommandStatus = Literal[
    "success",
    "spawn_error",
    "nonzero_exit",
    "timeout",
    "output_limit",
    "invalid_output",
    "deadline_exhausted",
    "skill_resolution_error",
    "skill_resolution_timeout",
]
RunCommandPhase = Literal["skill_resolution", "execution"]


@dataclass(frozen=True)
class RunCommandResult:
    """Metadata-only result from a bounded detector subprocess."""

    status: RunCommandStatus
    context: str | None
    duration_ms: float
    exit_code: int | None
    stdout_bytes: int
    stderr_bytes: int
    timeout_seconds: float
    overflow_stream: Literal["stdout", "stderr"] | None
    background: bool
    phase: RunCommandPhase
    skill: str | None
    script: str | None

    @classmethod
    def deadline_exhausted(
        cls,
        *,
        timeout_seconds: float,
        background: bool = False,
        skill: str | None = None,
        script: str | None = None,
    ) -> RunCommandResult:
        return cls(
            status="deadline_exhausted",
            context=None,
            duration_ms=0.0,
            exit_code=None,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=timeout_seconds,
            overflow_stream=None,
            background=background,
            phase="execution",
            skill=skill,
            script=script,
        )

    @classmethod
    def skill_resolution_failure(
        cls,
        status: Literal["skill_resolution_error", "skill_resolution_timeout"],
        *,
        started: float,
        timeout_seconds: float,
        background: bool,
        skill: str,
        script: str,
    ) -> RunCommandResult:
        return cls(
            status=status,
            context=None,
            duration_ms=(time.perf_counter() - started) * 1000,
            exit_code=None,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=timeout_seconds,
            overflow_stream=None,
            background=background,
            phase="skill_resolution",
            skill=skill,
            script=script,
        )


class _StreamOverflow(Exception):
    def __init__(self, stream: Literal["stdout", "stderr"]) -> None:
        self.stream = stream


def resolve_materialized_skill_script(scripts_dir: Path, script: str) -> Path:
    """Resolve a script while enforcing containment in a materialized scripts directory."""
    posix = PurePosixPath(script)
    windows = PureWindowsPath(script)
    if not script.strip() or posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("Skill script path must be non-empty and relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError("Skill script path cannot traverse its scripts directory")

    root = scripts_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Materialized scripts path is not a directory")
    target = (root / script).resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Skill script resolves outside its scripts directory") from exc
    if not target.is_file():
        raise ValueError("Skill script path must name a file")
    return target


def build_run_command_payload(event: HookEvent) -> dict[str, object]:
    """Deep-copy raw provider data and add normalized detector fields."""
    payload = cast(dict[str, object], copy.deepcopy(event.data))
    normalize_tool_fields(payload)
    raw_cwd = event.cwd or payload.get("cwd")
    payload["cwd"] = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else os.getcwd()
    if "hook_event_name" not in payload:
        payload["hook_event_name"] = (
            "PostToolUse" if event.event_type == HookEventType.AFTER_TOOL else "Stop"
        )
    return payload


async def execute_run_command(
    command: list[str],
    *,
    cwd: str,
    stdin_payload: bytes,
    timeout_seconds: float,
    background: bool,
    environment: Mapping[str, str] | None = None,
) -> RunCommandResult:
    """Run a command with capped concurrent output reads and deterministic cleanup."""
    started = time.perf_counter()
    subprocess_environment = os.environ.copy()
    if environment:
        subprocess_environment.update(environment)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=subprocess_environment,
        )
    except (OSError, ValueError):
        return _result(
            "spawn_error",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
        )

    if process.stdin is None or process.stdout is None or process.stderr is None:
        await _kill_and_reap(process, [])
        return _result(
            "spawn_error",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
        )

    byte_counts: dict[Literal["stdout", "stderr"], int] = {"stdout": 0, "stderr": 0}
    stdout_task = asyncio.create_task(
        _read_capped(process.stdout, STDOUT_LIMIT_BYTES, "stdout", byte_counts)
    )
    stderr_task = asyncio.create_task(
        _read_capped(process.stderr, STDERR_LIMIT_BYTES, "stderr", byte_counts)
    )
    tasks: list[asyncio.Task[object]] = [
        cast(asyncio.Task[object], asyncio.create_task(_write_stdin(process.stdin, stdin_payload))),
        cast(asyncio.Task[object], stdout_task),
        cast(asyncio.Task[object], stderr_task),
        cast(asyncio.Task[object], asyncio.create_task(process.wait())),
    ]

    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout_seconds)
    except TimeoutError:
        await _kill_and_reap(process, tasks)
        return _result(
            "timeout",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
            stdout_bytes=byte_counts["stdout"],
            stderr_bytes=byte_counts["stderr"],
        )
    except _StreamOverflow as exc:
        await _kill_and_reap(process, tasks)
        return _result(
            "output_limit",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
            stdout_bytes=byte_counts["stdout"],
            stderr_bytes=byte_counts["stderr"],
            overflow_stream=exc.stream,
        )
    except (BrokenPipeError, ConnectionResetError, OSError):
        await _kill_and_reap(process, tasks)
        return _result(
            "spawn_error",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
            stdout_bytes=byte_counts["stdout"],
            stderr_bytes=byte_counts["stderr"],
        )
    except Exception:
        await _kill_and_reap(process, tasks)
        return _result(
            "spawn_error",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
            stdout_bytes=byte_counts["stdout"],
            stderr_bytes=byte_counts["stderr"],
        )

    stdout = stdout_task.result()
    exit_code = process.returncode
    if exit_code != 0:
        return _result(
            "nonzero_exit",
            started=started,
            timeout_seconds=timeout_seconds,
            background=background,
            exit_code=exit_code,
            stdout_bytes=byte_counts["stdout"],
            stderr_bytes=byte_counts["stderr"],
        )

    output_valid, context = _parse_command_output(stdout)
    status: RunCommandStatus = "success" if output_valid else "invalid_output"
    return _result(
        status,
        started=started,
        timeout_seconds=timeout_seconds,
        background=background,
        context=context,
        exit_code=exit_code,
        stdout_bytes=byte_counts["stdout"],
        stderr_bytes=byte_counts["stderr"],
    )


async def _read_capped(
    stream: asyncio.StreamReader,
    limit: int,
    name: Literal["stdout", "stderr"],
    byte_counts: dict[Literal["stdout", "stderr"], int],
) -> bytes:
    chunks: list[bytes] = []
    while chunk := await stream.read(64 * 1024):
        byte_counts[name] += len(chunk)
        if byte_counts[name] > limit:
            raise _StreamOverflow(name)
        chunks.append(chunk)
    return b"".join(chunks)


async def _write_stdin(stream: asyncio.StreamWriter, payload: bytes) -> None:
    try:
        stream.write(payload)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _kill_and_reap(
    process: asyncio.subprocess.Process,
    tasks: list[asyncio.Task[object]],
) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await process.wait()


def _parse_command_output(stdout: bytes) -> tuple[bool, str | None]:
    if not stdout.strip():
        return True, None
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False, None
    if not isinstance(parsed, dict):
        return False, None
    hook_specific = parsed.get("hookSpecificOutput")
    if isinstance(hook_specific, dict):
        context = hook_specific.get("additionalContext")
        if isinstance(context, str) and context.strip():
            return True, context
    context = parsed.get("additionalContext")
    if isinstance(context, str) and context.strip():
        return True, context
    return True, None


def _result(
    status: RunCommandStatus,
    *,
    started: float,
    timeout_seconds: float,
    background: bool,
    context: str | None = None,
    exit_code: int | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    overflow_stream: Literal["stdout", "stderr"] | None = None,
    skill: str | None = None,
    script: str | None = None,
) -> RunCommandResult:
    return RunCommandResult(
        status=status,
        context=context,
        duration_ms=(time.perf_counter() - started) * 1000,
        exit_code=exit_code,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        timeout_seconds=timeout_seconds,
        overflow_stream=overflow_stream,
        background=background,
        phase="execution",
        skill=skill,
        script=script,
    )
