"""Hook terminal-context helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psutil

_DROID_PROCESS_NAME = "droid"


def is_gobby_acp_child(terminal_context: object) -> bool:
    """Return whether terminal metadata marks a daemon-owned ACP child process."""
    return isinstance(terminal_context, Mapping) and terminal_context.get("gobby_acp_child") == "1"


def hook_cwd(data: Mapping[str, Any], event_cwd: Any = None) -> str | None:
    """Return the first non-empty cwd supplied by hook data or event metadata."""
    return _non_empty_str(data.get("cwd")) or _non_empty_str(event_cwd)


def enrich_terminal_context_with_cwd(
    terminal_context: dict[str, Any] | None,
    cwd: Any,
) -> dict[str, Any] | None:
    """Copy terminal context and add cwd and parent-process identity."""
    cwd_text = _non_empty_str(cwd)
    if terminal_context is None:
        return {"cwd": cwd_text} if cwd_text else None

    enriched = dict(terminal_context)
    if cwd_text and not _non_empty_str(enriched.get("cwd")):
        enriched["cwd"] = cwd_text
    _record_parent_process_identity(enriched)
    return enriched


def _record_parent_process_identity(terminal_context: dict[str, Any]) -> None:
    terminal_context.pop("parent_create_time", None)
    terminal_context.pop("parent_name", None)

    parent_pid = terminal_context.get("parent_pid")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, (int, str)):
        return
    try:
        pid = int(parent_pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return

    try:
        hook_parent = psutil.Process(pid)
        process = _stable_cli_process(hook_parent)
        if process is not hook_parent:
            terminal_context["parent_pid"] = process.pid
        terminal_context["parent_create_time"] = process.create_time()
        terminal_context["parent_name"] = process.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        # An unverifiable pid is no identity: a CLI process's last hook as it exits would
        # otherwise merge its pid over the session's verified pid and create time.
        terminal_context.pop("parent_pid", None)
        terminal_context.pop("parent_create_time", None)
        terminal_context.pop("parent_name", None)


def _stable_cli_process(process: psutil.Process) -> psutil.Process:
    """Return the CLI process that outlives the session boundaries.

    Droid's TUI runs a ``droid exec`` backend child that runs the hooks, and it replaces
    that backend on ``/compress`` and ``/clear``. The TUI is the terminal's identity.
    A headless ``droid exec``, including one under another ``droid exec``, stays its own.
    """
    if process.name() != _DROID_PROCESS_NAME:
        return process
    try:
        parent = process.parent()
        if (
            parent is not None
            and parent.name() == _DROID_PROCESS_NAME
            and parent.cmdline()[1:2] != ["exec"]
        ):
            return parent
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        pass
    return process


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None
