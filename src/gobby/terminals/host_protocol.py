"""Control-protocol constants, paths, and list-row types for gterm."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CONTROL_PROTOCOL_VERSION = 1
CONTROL_SOCKET_NAME = "gterm-control.sock"
FRAMES_SOCKET_NAME = "gterm-frames.sock"
PID_FILE_NAME = "gterm.pid"
CONTROL_TOKEN_FILE_NAME = "gterm-control.token"
HOST_LOG_NAME = "gterm.log"


def expand_socket_dir(socket_dir: str | Path) -> Path:
    """Resolve the host socket directory, expanding ``~``."""
    return Path(socket_dir).expanduser()


def control_socket_path(socket_dir: str | Path) -> Path:
    return expand_socket_dir(socket_dir) / CONTROL_SOCKET_NAME


def frames_socket_path(socket_dir: str | Path) -> Path:
    return expand_socket_dir(socket_dir) / FRAMES_SOCKET_NAME


def pidfile_path(socket_dir: str | Path) -> Path:
    return expand_socket_dir(socket_dir) / PID_FILE_NAME


def control_token_path(socket_dir: str | Path) -> Path:
    return expand_socket_dir(socket_dir) / CONTROL_TOKEN_FILE_NAME


def write_pidfile(socket_dir: str | Path, pid: int) -> Path:
    """Write ``gterm.pid`` with the serving process id."""
    path = pidfile_path(socket_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")
    return path


def read_pidfile(socket_dir: str | Path) -> int | None:
    path = pidfile_path(socket_dir)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def encode_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def decode_line(line: str) -> dict[str, Any]:
    parsed: object = json.loads(line)
    if not isinstance(parsed, dict):
        raise ValueError("control response must be an object")
    return parsed


@dataclass(frozen=True)
class HostListRow:
    terminal_id: str
    spawn_key: str
    commit_state: Literal["prepared", "committed"]
    observer_bind: Literal["reserved", "none"]
    host_terminal_id: str
    pgid: int | None = None
    start_time: float | None = None
    observation_state: str = "live"
    observation_reason: str | None = None
    observation_generation: int = 1
    tmux_history_bytes: int = 0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> HostListRow:
        commit = raw.get("commit_state", "committed")
        bind = raw.get("observer_bind", "none")
        if commit not in ("prepared", "committed"):
            commit = "committed"
        if bind not in ("reserved", "none"):
            bind = "none"
        pgid_raw = raw.get("pgid")
        start_raw = raw.get("start_time")
        return cls(
            terminal_id=str(raw["terminal_id"]),
            spawn_key=str(raw["spawn_key"]),
            commit_state=commit,
            observer_bind=bind,
            host_terminal_id=str(raw.get("host_terminal_id") or raw["terminal_id"]),
            pgid=int(pgid_raw) if isinstance(pgid_raw, int) else None,
            start_time=float(start_raw) if isinstance(start_raw, (int, float)) else None,
            observation_state=str(raw.get("observation_state") or "live"),
            observation_reason=(
                str(reason) if (reason := raw.get("observation_reason")) is not None else None
            ),
            observation_generation=int(raw.get("observation_generation") or 1),
            tmux_history_bytes=int(raw.get("tmux_history_bytes") or 0),
        )


def row_identity(row: Any) -> tuple[str, str]:
    return (str(row.terminal_id), str(row.spawn_key))


def atomic_replace_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)
    os.chmod(path, mode)
