"""Literal text injection helpers for tmux targets."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import suppress
from shlex import join as shell_join
from typing import Literal
from uuid import uuid4

TMUX_TEXT_INJECTION_TIMEOUT_SECONDS = 10.0
TMUX_TEXT_ENTER_DELAY_SECONDS = 1.0
TMUX_TEXT_ENTER_RETRY_DELAY_SECONDS = 0.25

# tmux rejects any command whose imsg exceeds MAX_IMSGSIZE with "command too long".
# Measured against tmux 3.x: a 16000-byte set-buffer payload succeeds, 20000 fails.
# 8192 leaves room for the argv around the payload and for a long tmux_cmd prefix.
TMUX_BUFFER_CHUNK_BYTES = 8192

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
_ATTENTION_KEYS = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "up": "Up",
    "down": "Down",
}

AttentionInjectionStage = Literal["none", "partial"]


class AttentionInjectionError(RuntimeError):
    """An attention answer failed before or after payload delivery."""

    def __init__(self, *, stage: AttentionInjectionStage) -> None:
        super().__init__(f"attention injection failed at stage {stage}")
        self.stage = stage


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
        await paste_literal_text_to_tmux_target(
            target,
            literal_text,
            tmux_cmd=base_cmd,
            timeout=timeout,
        )

    if send_enter:
        if literal_text and enter_delay_seconds > 0:
            await asyncio.sleep(enter_delay_seconds)
        try:
            await send_enter_key_to_tmux_target(
                target,
                tmux_cmd=base_cmd,
                timeout=timeout,
            )
        except TmuxTextInjectionTimeout:
            if not literal_text:
                raise
            await asyncio.sleep(TMUX_TEXT_ENTER_RETRY_DELAY_SECONDS)
            await send_enter_key_to_tmux_target(
                target,
                tmux_cmd=base_cmd,
                timeout=timeout,
            )


async def paste_literal_text_to_tmux_target(
    target: str,
    text: str,
    *,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
) -> None:
    """Paste exact literal text without interpreting trailing newlines."""
    if not text:
        return
    base_cmd = tuple(tmux_cmd)
    buffer_name = f"gobby-{os.getpid()}-{uuid4().hex}"
    head, *tail = _split_for_tmux_buffer(text)
    await _run_tmux_command(
        (*base_cmd, "set-buffer", "-b", buffer_name, "--", head),
        timeout=timeout,
    )
    try:
        for chunk in tail:
            await _run_tmux_command(
                (*base_cmd, "set-buffer", "-a", "-b", buffer_name, "--", chunk),
                timeout=timeout,
            )
        await _run_tmux_command(
            (
                *base_cmd,
                "paste-buffer",
                "-d",
                "-p",
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


async def inject_attention_answer_to_tmux_target(
    target: str,
    *,
    option: int | None = None,
    text: str | None = None,
    key: str | None = None,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
    enter_delay_seconds: float = TMUX_TEXT_ENTER_DELAY_SECONDS,
) -> None:
    """Inject one validated attention answer and retain partial-send evidence."""
    variants = sum(value is not None for value in (option, text, key))
    if variants != 1:
        raise ValueError("exactly one attention answer variant is required")

    base_cmd = tuple(tmux_cmd)
    if key is not None:
        named_key = _ATTENTION_KEYS.get(key)
        if named_key is None:
            raise ValueError(f"unsupported attention key: {key}")
        try:
            await send_named_key_to_tmux_target(
                target,
                named_key,
                tmux_cmd=base_cmd,
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AttentionInjectionError(stage="none") from exc
        return

    payload = str(option) if option is not None else text or ""
    payload_landed = False
    try:
        if payload:
            await paste_literal_text_to_tmux_target(
                target,
                payload,
                tmux_cmd=base_cmd,
                timeout=timeout,
            )
            payload_landed = True
            if enter_delay_seconds > 0:
                await asyncio.sleep(enter_delay_seconds)
        await send_enter_key_to_tmux_target(
            target,
            tmux_cmd=base_cmd,
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        stage: AttentionInjectionStage = "partial" if payload_landed else "none"
        raise AttentionInjectionError(stage=stage) from exc


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
            enter_delay_seconds=enter_delay_seconds,
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


async def send_named_key_to_tmux_target(
    target: str,
    key: str,
    *,
    tmux_cmd: Sequence[str] = ("tmux",),
    timeout: float = TMUX_TEXT_INJECTION_TIMEOUT_SECONDS,
) -> None:
    """Send one prevalidated tmux key name."""
    await _run_tmux_command(
        (*tuple(tmux_cmd), "send-keys", "-t", target, key),
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


def _split_for_tmux_buffer(
    text: str,
    *,
    limit: int = TMUX_BUFFER_CHUNK_BYTES,
) -> list[str]:
    """Split text so each chunk's UTF-8 encoding fits one tmux command.

    Chunks are cut on code-point boundaries, so every chunk is independently
    encodable and the concatenation is byte-identical to the input.
    """
    encoded = text.encode()
    if len(encoded) <= limit:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        # Back off any UTF-8 continuation byte so a code point is never split.
        # A sequence is at most 4 bytes, so this moves `end` by at most 3 and
        # cannot reach `start` for any sane limit.
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode())
        start = end
    return chunks


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
