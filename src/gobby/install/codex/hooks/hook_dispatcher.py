#!/usr/bin/env python3
"""Codex notify script - forwards interactive Codex events to gobby.

Codex supports a `notify = [...]` command in `~/.codex/config.toml`. This script is
intended to be installed to `~/.gobby/hooks/codex/notify.py` and configured as that
notify command.

Codex notify is currently treated as fire-and-forget; this script should never
block the CLI for long or fail the Codex command on daemon errors.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiofiles
import httpx

DEFAULT_DAEMON_PORT = 60887
DEFAULT_BOOTSTRAP_PATH = "~/.gobby/bootstrap.yaml"
DEBUG_ENV_VAR = "GOBBY_CODEX_NOTIFY_DEBUG"


def _silence_output() -> None:
    """Prevent notify script output from polluting the interactive Codex UI.

    Uses os.devnull file descriptor directly to avoid blocking async-unsafe open().
    This is safe to call from async context because os.open/os.fdopen are thin
    wrappers around C-level fd operations that do not block the event loop.
    """
    if os.environ.get(DEBUG_ENV_VAR):
        return

    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        devnull = os.fdopen(fd, "w", encoding="utf-8")
        sys.stdout = devnull
        sys.stderr = devnull
    except (OSError, AttributeError):
        pass  # devnull may be unavailable (OSError) or std streams unwritable (AttributeError)


_cached_daemon_url: str | None = None


async def _get_daemon_url() -> str:
    global _cached_daemon_url  # noqa: PLW0603
    if _cached_daemon_url is not None:
        return _cached_daemon_url

    bootstrap_path = Path(DEFAULT_BOOTSTRAP_PATH).expanduser()

    port = DEFAULT_DAEMON_PORT
    if bootstrap_path.exists():
        try:
            import yaml

            async with aiofiles.open(bootstrap_path, encoding="utf-8") as f:
                content = await f.read()
            config = yaml.safe_load(content) or {}
            port = int(config.get("daemon_port", DEFAULT_DAEMON_PORT))
        except Exception:
            port = DEFAULT_DAEMON_PORT

    _cached_daemon_url = f"http://localhost:{port}"
    return _cached_daemon_url


async def _read_event_from_stdin() -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(None, sys.stdin.read)
    except Exception:
        return None

    if not raw.strip():
        return None

    try:
        parsed = json.loads(raw)
    except Exception:
        return {"raw": raw}

    if isinstance(parsed, dict):
        return parsed
    return {"event": parsed}


def _extract_text_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        text = message.get("text") or message.get("content")
        if isinstance(text, str) and text:
            return text
    return ""


def _normalize_input_data(event: dict[str, Any] | None) -> dict[str, Any]:
    event = event or {}

    thread_id = (
        event.get("session_id")
        or event.get("thread_id")
        or event.get("threadId")
        or event.get("conversation_id")
        or event.get("conversationId")
    )
    if not thread_id and isinstance(event.get("thread"), dict):
        thread_id = event["thread"].get("id")
    if not thread_id and isinstance(event.get("session"), dict):
        thread_id = event["session"].get("id")

    messages = event.get("input_messages") or event.get("inputMessages") or event.get("messages")
    last_message = (
        event.get("last_message")
        or event.get("lastMessage")
        or event.get("message")
        or _extract_text_from_messages(messages)
    )

    event_type = (
        event.get("event_type")
        or event.get("eventType")
        or event.get("type")
        or event.get("name")
        or "agent-turn-complete"
    )

    return {
        "session_id": thread_id or "",
        "event_type": event_type,
        "last_message": last_message or "",
        "input_messages": messages if isinstance(messages, list) else [],
    }


async def main() -> int:
    _silence_output()

    event = await _read_event_from_stdin()
    input_data = _normalize_input_data(event)

    daemon_url = await _get_daemon_url()

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{daemon_url}/api/hooks/execute",
                json={
                    "hook_type": "AgentTurnComplete",
                    "input_data": input_data,
                    "source": "codex",
                },
                timeout=2.0,
            )
    except Exception:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
