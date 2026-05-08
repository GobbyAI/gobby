#!/usr/bin/env python3
"""Guard native ghook dispatch for intentional daemon shutdown windows."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import yaml

DEFAULT_DAEMON_URL = "http://localhost:60887"
DEFAULT_ALLOW_SECONDS = 120.0
STOP_HOOK_TYPES = {"stop"}


def main() -> int:
    ghook_args = _ghook_args(sys.argv[1:])

    if not ghook_args:
        sys.stderr.write("ghook_guard: missing ghook command\n")
        return 2

    if not _child_command_available(ghook_args[0]):
        sys.stderr.write(f"ghook_guard: command not found or not executable: {ghook_args[0]}\n")
        return 127

    stdin_bytes = sys.stdin.buffer.read()

    if _is_stop_hook(ghook_args) and not _daemon_is_reachable() and _fresh_shutdown_marker():
        sys.stdout.write(json.dumps({"continue": True}))
        sys.stdout.write("\n")
        return 0

    result = subprocess.run(ghook_args, input=stdin_bytes, check=False)  # noqa: S603
    return int(result.returncode)


def _ghook_args(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _child_command_available(command: str) -> bool:
    if not command:
        return False
    if os.sep in command or (os.altsep and os.altsep in command):
        return Path(command).is_file() and os.access(command, os.X_OK)
    return shutil.which(command) is not None


def _is_stop_hook(args: list[str]) -> bool:
    hook_type: str | None = None
    for index, arg in enumerate(args):
        if arg == "--type" and index + 1 < len(args):
            hook_type = args[index + 1]
            break
        if arg.startswith("--type="):
            hook_type = arg.split("=", 1)[1]
            break
    return hook_type is not None and hook_type.strip().lower() in STOP_HOOK_TYPES


def _daemon_is_reachable() -> bool:
    target = _daemon_health_target()
    if target is None:
        return False

    scheme, host, port, path = target
    connection_cls = (
        http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_cls(host, port=port, timeout=0.35)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return 200 <= int(response.status) < 500
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _daemon_health_target() -> tuple[str, str, int | None, str] | None:
    url = _daemon_url().rstrip("/") + "/api/admin/health"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None

    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return parsed.scheme, parsed.hostname, port, path


def _daemon_url() -> str:
    env_url = os.environ.get("GOBBY_DAEMON_URL")
    if env_url:
        return env_url

    bootstrap = _gobby_home() / "bootstrap.yaml"
    try:
        data = bootstrap.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_DAEMON_URL

    host = _yaml_scalar(data, "bind_host") or "localhost"
    port = _yaml_scalar(data, "daemon_port") or "60887"
    return f"http://{host}:{port}"


def _yaml_scalar(data: str, key: str) -> str | None:
    try:
        loaded = yaml.safe_load(data)
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    value = loaded.get(key)
    if value is None:
        return None
    value_str = str(value)
    return value_str or None


def _fresh_shutdown_marker() -> bool:
    max_age = _allow_seconds()
    for marker in (
        _gobby_home() / "shutdown_intent_active.json",
        _gobby_home() / "shutdown_source.json",
    ):
        data = _read_marker(marker)
        if data is None:
            continue
        timestamp = _optional_float(data.get("timestamp"))
        if timestamp is None or time.time() - timestamp > max_age:
            continue
        intent = str(data.get("intent") or "").lower()
        source = str(data.get("source") or "").lower()
        if intent in {"stop", "restart"} or source.startswith(("cli_", "http_", "service_")):
            return True
    return False


def _read_marker(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _allow_seconds() -> float:
    value = _optional_float(os.environ.get("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS"))
    return value if value is not None and value > 0 else DEFAULT_ALLOW_SECONDS


def _optional_float(value: object) -> float | None:
    if not isinstance(value, str | int | float):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _gobby_home() -> Path:
    return Path(os.environ.get("GOBBY_HOME", str(Path.home() / ".gobby")))


if __name__ == "__main__":
    raise SystemExit(main())
