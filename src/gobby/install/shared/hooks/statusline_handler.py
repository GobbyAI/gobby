#!/usr/bin/env python3
"""Statusline middleware for Claude Code.

Consumes Claude Code's statusLine JSON feed, posts usage data to the Gobby
daemon, and optionally forwards the original JSON to a downstream display
command (e.g., cship).

Stdlib-only — no uv run needed for fast startup.

Usage (set in Claude Code settings.json):
    statusLine: {
        "type": "command",
        "command": "python3 ~/.gobby/hooks/statusline_handler.py"
    }

Environment variables:
    GOBBY_STATUSLINE_DOWNSTREAM: Command to forward JSON to (optional)
"""

import json
import os
import re
import subprocess  # nosec B404
import sys
import threading
import urllib.request  # nosec B404
from datetime import UTC, datetime
from typing import Any

_DEFAULT_PORT = 60887
_BOOTSTRAP_PATH = os.path.expanduser("~/.gobby/bootstrap.yaml")
_ERROR_LOG_PATH = os.path.expanduser("~/.gobby/logs/statusline_handler_errors.log")


def _log_handler_error(stage: str, error: BaseException | str) -> None:
    """Write a best-effort error line without affecting Claude Code statusLine."""
    try:
        os.makedirs(os.path.dirname(_ERROR_LOG_PATH), exist_ok=True)
        message = str(error).replace("\n", "\\n")
        timestamp = datetime.now(UTC).isoformat()
        with open(_ERROR_LOG_PATH, "a") as f:
            f.write(f"{timestamp} statusline_handler_error stage={stage} error={message}\n")
    except Exception:
        pass


def _read_daemon_port() -> int:
    """Read daemon port from bootstrap.yaml using regex (no PyYAML dep)."""
    try:
        with open(_BOOTSTRAP_PATH) as f:
            content = f.read()
        match = re.search(r"daemon_port:\s*(\d+)", content)
        if match:
            return int(match.group(1))
    except (OSError, ValueError):
        pass
    return _DEFAULT_PORT


def _post_to_daemon(port: int, payload: bytes) -> None:
    """Fire-and-forget POST to daemon statusline endpoint."""
    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/api/sessions/statusline",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)  # nosec B310
    except Exception as e:
        _log_handler_error("post_to_daemon", e)


def _extract_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the token usage fields we care about from Claude Code's statusline JSON."""
    session_id = data.get("session_id")
    if not session_id:
        return None

    cost = data.get("cost", {})
    if not isinstance(cost, dict):
        cost = {}

    model_info = data.get("model", {})
    context_window = data.get("context_window", {})

    return {
        "session_id": session_id,
        "model_id": model_info.get("id", ""),
        "input_tokens": cost.get("input_tokens", 0),
        "output_tokens": cost.get("output_tokens", 0),
        "cache_creation_tokens": cost.get("cache_creation_tokens", 0),
        "cache_read_tokens": cost.get("cache_read_tokens", 0),
        "context_window_size": context_window.get("size", 0),
    }


def _forward_downstream(command: str, raw_json: str) -> None:
    """Spawn downstream command, pipe original JSON, relay stdout."""
    try:
        proc = subprocess.Popen(  # nosec B602
            command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = proc.communicate(input=raw_json.encode(), timeout=5)
        if stdout:
            sys.stdout.buffer.write(stdout)
            sys.stdout.buffer.flush()
    except subprocess.TimeoutExpired as e:
        try:
            proc.kill()
        except OSError:
            pass
        _log_handler_error("forward_downstream", e)
    except OSError as e:
        _log_handler_error("forward_downstream", e)


def main() -> int:
    """Main entry point."""
    try:
        raw = sys.stdin.read()
    except Exception as e:
        _log_handler_error("read_stdin", e)
        return 0

    # Parse JSON
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        _log_handler_error("parse_json", e)
        return 0

    # Extract and POST to daemon in a background thread
    payload = _extract_payload(data)
    if payload:
        port = _read_daemon_port()
        payload_bytes = json.dumps(payload).encode()
        thread = threading.Thread(target=_post_to_daemon, args=(port, payload_bytes), daemon=True)
        thread.start()

    # Forward to downstream if configured
    downstream = os.environ.get("GOBBY_STATUSLINE_DOWNSTREAM")
    if downstream:
        _forward_downstream(downstream, raw)

    # Wait briefly for the POST thread to complete
    if payload:
        thread.join(timeout=0.3)

    return 0


if __name__ == "__main__":
    sys.exit(main())
