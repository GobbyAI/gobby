#!/usr/bin/env python3
"""Codex notify script - forwards interactive Codex events to gobby-daemon.

Codex supports a `notify = [...]` command in `~/.codex/config.toml`. This script is
intended to be installed to `~/.gobby/hooks/codex/notify.py` and configured as that
notify command.

Codex notify is currently treated as fire-and-forget; this script should never
block the CLI for long or fail the Codex command on daemon errors.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

DEFAULT_DAEMON_PORT = 8765
DEFAULT_CONFIG_PATH = "~/.gobby/config.yaml"
DEBUG_ENV_VAR = "GOBBY_CODEX_NOTIFY_DEBUG"


def _silence_output() -> None:
    """Prevent notify script output from polluting the interactive Codex UI."""
    if os.environ.get(DEBUG_ENV_VAR):
        return

    try:
        devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        sys.stdout = devnull
        sys.stderr = devnull
    except Exception:
        # If silencing fails, still avoid raising/printing.
        pass


def _get_daemon_url() -> str:
    config_path = Path(DEFAULT_CONFIG_PATH).expanduser()

    port = DEFAULT_DAEMON_PORT
    if config_path.exists():
        try:
            import yaml

            with config_path.open(encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            port = int(config.get("daemon_port", DEFAULT_DAEMON_PORT))
        except Exception:
            port = DEFAULT_DAEMON_PORT

    return f"http://localhost:{port}"


def _read_event_from_stdin() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
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


def main() -> int:
    _silence_output()

    event = _read_event_from_stdin()
    input_data = _normalize_input_data(event)

    daemon_url = _get_daemon_url()

    try:
        httpx.post(
            f"{daemon_url}/hooks/execute",
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
    raise SystemExit(main())
