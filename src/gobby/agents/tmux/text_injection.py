"""Literal text injection helpers for tmux targets."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import suppress
from shlex import join as shell_join
from uuid import uuid4

TMUX_TEXT_INJECTION_TIMEOUT_SECONDS = 10.0
TMUX_TEXT_ENTER_DELAY_SECONDS = 0.05

_MISSING_OR_DEAD_TARGET_FRAGMENTS = (
    "can't find pane",
    "can't find session",
    "dead pane",
    "no server running",
    "no such pane",
    "no such session",
    "pane is dead",
    "pane_dead",
)
_PANE_MODE_UNAVAILABLE_FRAGMENTS = ("not in a mode",)


class TmuxTextInjectionError(RuntimeError):
    """Base error for tmux literal text injection failures."""

    error_code = "tmux_command_failed"
    expected = False

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.stderr = stderr
        self.returncode = returncode


class TmuxExpectedTextInjectionError(TmuxTextInjectionError):
    """Expected best-effort tmux delivery failure."""

    expected = True


class TmuxTextInjectionTimeout(TmuxExpectedTextInjectionError):
    """tmux command timed out during literal text injection."""

    error_code = "tmux_command_timeout"

    def __init__(self, *, command: Sequence[str], timeout: float) -> None:
        super().__init__(
            f"tmux command timed out after {timeout:g}s: {shell_join(command)}",
            command=command,
        )
        self.timeout = timeout


class TmuxTargetUnavailableError(TmuxExpectedTextInjectionError):
    """tmux target pane/session is missing or dead."""

    error_code = "tmux_target_unavailable"


class TmuxPaneModeUnavailableError(TmuxExpectedTextInjectionError):
    """tmux target rejected a mode-specific command."""

    error_code = "tmux_pane_mode_unavailable"


def classify_tmux_text_injection_error(
    command: Sequence[str],
    returncode: int,
    stderr: str,
) -> TmuxTextInjectionError:
    """Classify a failed tmux literal text command."""
    detail = stderr.strip() or f"tmux exited with status {returncode}"
    message = detail.lower()
    if any(fragment in message for fragment in _MISSING_OR_DEAD_TARGET_FRAGMENTS):
        return TmuxTargetUnavailableError(
            f"tmux target is unavailable: {detail}",
            command=command,
            stderr=stderr,
            returncode=returncode,
        )
    if any(fragment in message for fragment in _PANE_MODE_UNAVAILABLE_FRAGMENTS):
        return TmuxPaneModeUnavailableError(
            f"tmux target is not accepting pane input: {detail}",
            command=command,
            stderr=stderr,
            returncode=returncode,
        )
    return TmuxTextInjectionError(
        f"tmux command failed with status {returncode}: {detail}",
        command=command,
        stderr=stderr,
        returncode=returncode,
    )


async def send_literal_text_to_tmux_target(
    target: str,
    text: str,
    *,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
    enter_delay_seconds: float = TMUX_TEXT_ENTER_DELAY_SECONDS,
) -> None:
    """Paste literal text into a tmux target, then optionally press Enter."""
    send_enter = text.endswith("\n")
    literal_text = text.rstrip("\n")
    base_cmd = tuple(tmux_cmd)

    if literal_text:
        buffer_name = f"gobby-{os.getpid()}-{uuid4().hex}"
        await _run_tmux_command(
            (
                *base_cmd,
                "set-buffer",
                "-b",
                buffer_name,
                "--",
                literal_text,
            ),
            timeout=timeout,
        )
        try:
            await _run_tmux_command(
                (
                    *base_cmd,
                    "paste-buffer",
                    "-d",
                    "-b",
                    buffer_name,
                    "-t",
                    target,
                ),
                timeout=timeout,
            )
        finally:
            try:
                await _run_tmux_command(
                    (*base_cmd, "delete-buffer", "-b", buffer_name),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    if send_enter:
        if literal_text and enter_delay_seconds > 0:
            await asyncio.sleep(enter_delay_seconds)
        await send_enter_key_to_tmux_target(
            target,
            tmux_cmd=base_cmd,
            timeout=timeout,
        )


async def submit_literal_text_to_tmux_target(
    target: str,
    text: str,
    *,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
    enter_delay_seconds: float = TMUX_TEXT_ENTER_DELAY_SECONDS,
    escape_before_submit: bool = False,
) -> None:
    """Submit non-empty literal text through buffer paste; empty text sends raw Enter."""
    literal_text = text.rstrip("\n")
    base_cmd = tuple(tmux_cmd)

    if escape_before_submit:
        await send_escape_key_to_tmux_target(
            target,
            tmux_cmd=base_cmd,
            timeout=timeout,
        )
        if literal_text and enter_delay_seconds > 0:
            await asyncio.sleep(enter_delay_seconds)

    if literal_text:
        await send_literal_text_to_tmux_target(
            target,
            f"{literal_text}\n",
            tmux_cmd=base_cmd,
            timeout=timeout,
        )
    else:
        await send_enter_key_to_tmux_target(
            target,
            tmux_cmd=base_cmd,
            timeout=timeout,
        )


async def send_escape_key_to_tmux_target(
    target: str,
    *,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
) -> None:
    """Send Escape as a tmux key event, not pasted literal text."""
    await _run_tmux_command(
        (*tuple(tmux_cmd), "send-keys", "-t", target, "Escape"),
        timeout=timeout,
    )


async def send_enter_key_to_tmux_target(
    target: str,
    *,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
) -> None:
    """Send Enter as a tmux key event, not pasted literal text."""
    await _run_tmux_command(
        (*tuple(tmux_cmd), "send-keys", "-t", target, "Enter"),
        timeout=timeout,
    )


async def _run_tmux_command(command: Sequence[str], *, timeout: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            await proc.communicate()
        raise TmuxTextInjectionTimeout(command=command, timeout=timeout) from None

    returncode = proc.returncode if proc.returncode is not None else 0
    if returncode != 0:
        stderr = (stderr_bytes or b"").decode(errors="replace")
        raise classify_tmux_text_injection_error(command, returncode, stderr)
