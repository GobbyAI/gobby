"""Reads the owner-only session credential record that a CLI's SessionStart stored.

The SessionStart hook writes one owner-only record per CLI process under
``<gobby home>/session-credentials/``. The file name is the UUIDv5 of the key
``cli-pid:<pid>`` in ``RECORD_NAMESPACE``, the same derivation gcore uses, so the
Rust hooks and this reader agree on the path without sharing code.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from gobby.hooks.terminal_context import _is_codex_shared_host, _stable_cli_process
from gobby.paths import get_gobby_home

RECORD_NAMESPACE = uuid.UUID("25855c0de00040008000000025855001")
STORE_DIR = "session-credentials"


@dataclass(frozen=True)
class SessionCredentialRecord:
    """A session token and the session it speaks for; the token never appears in a repr."""

    token: str = field(repr=False)
    session_id: str
    project_id: str


def cli_pid_key(pid: int) -> str:
    return f"cli-pid:{pid}"


def session_ref_key(session_ref: str) -> str:
    """Key of a seat's record under one of its own session refs (a shared-host seat)."""
    return f"session-ref:{session_ref}"


def record_path(home: Path, key: str) -> Path:
    return home / STORE_DIR / f"{uuid.uuid5(RECORD_NAMESPACE, key)}.json"


def load_record(home: Path, key: str) -> SessionCredentialRecord | None:
    """Return the record stored under ``key``, or None when it is absent or not owner-only."""
    path = record_path(home, key)
    try:
        status = path.stat()
        if status.st_uid != os.getuid() or status.st_mode & 0o077:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    session_id = payload.get("session_id")
    project_id = payload.get("project_id")
    if not isinstance(token, str) or not token or not isinstance(session_id, str) or not session_id:
        return None
    return SessionCredentialRecord(
        token=token,
        session_id=session_id,
        project_id=project_id if isinstance(project_id, str) else "",
    )


def stable_cli_pid() -> int | None:
    """Return the CLI process that owns this process, stabilized as the hooks stabilize it."""
    try:
        return int(_stable_cli_process(psutil.Process(os.getppid())).pid)
    except (psutil.Error, OSError):
        return None


def parent_is_codex_shared_host() -> bool:
    """Return whether Codex's shared app-server launched this process as its MCP client.

    Only that client's calls arrive as hook-gated tool calls, which the gate holds to the
    caller's own session ref. A proxy a seat starts from its shell has a shell parent.
    """
    try:
        return _is_codex_shared_host(psutil.Process(os.getppid()))
    except (psutil.Error, OSError):
        return False


def bound_session_credential(
    home: Path | None = None, session_ref: str | None = None
) -> SessionCredentialRecord | None:
    """Return the session credential bound to this process's CLI, or None when unbound.

    A CLI that attested itself keeps its record whatever ref a call names. Without one, a
    seat on a shared host is found by the session ref its call names, and only when the
    shared host launched this proxy: the hook gate refuses that client's calls naming any
    session other than the caller's own, while a shell-started proxy is not gated.
    """
    root = home if home is not None else get_gobby_home()
    pid = stable_cli_pid()
    record = load_record(root, cli_pid_key(pid)) if pid is not None else None
    if record is None and session_ref and session_ref.strip() and parent_is_codex_shared_host():
        record = load_record(root, session_ref_key(session_ref.strip()))
    return record
