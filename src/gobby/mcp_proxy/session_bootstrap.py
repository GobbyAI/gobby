"""Best-effort stdio session bootstrap helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

SESSION_BOOTSTRAP_TIMEOUT_SECONDS = 1.0


def read_project_id() -> str | None:
    """Read project_id from the environment or nearest .gobby/project.json."""
    env_project_id = os.environ.get("GOBBY_PROJECT_ID")
    if env_project_id:
        return env_project_id

    for root in [Path.cwd(), *Path.cwd().parents]:
        project_file = root / ".gobby" / "project.json"
        if not project_file.exists():
            continue
        try:
            data = json.loads(project_file.read_text())
        except (PermissionError, json.JSONDecodeError, OSError):
            return None
        project_id = data.get("id")
        return project_id if isinstance(project_id, str) else None
    return None


def current_terminal_context() -> dict[str, Any]:
    """Collect terminal identity signals available to the stdio proxy."""
    context: dict[str, Any] = {"parent_pid": os.getppid()}

    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        context["tmux_pane"] = tmux_pane

    tmux = os.environ.get("TMUX")
    if tmux:
        tmux_socket_path = tmux.split(",", 1)[0]
        if tmux_socket_path:
            context["tmux_socket_path"] = tmux_socket_path

    for env_name, context_name in (
        ("TMUX_SESSION", "tmux_session"),
        ("TTY", "tty"),
        ("TERM_PROGRAM", "term_program"),
        ("TERM_SESSION_ID", "term_session_id"),
    ):
        value = os.environ.get(env_name)
        if value:
            context[context_name] = value

    return context


async def resolve_session_id_from_terminal_context(
    base_url: str,
    project_id: str | None,
) -> str | None:
    """Ask the daemon for the one active session matching this stdio process."""
    if not project_id:
        return None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/api/sessions/find_by_terminal_context",
                json={
                    "project_id": project_id,
                    "parent_pid": os.getppid(),
                    "terminal_context": current_terminal_context(),
                },
                timeout=SESSION_BOOTSTRAP_TIMEOUT_SECONDS,
            )
    except (httpx.HTTPError, OSError):
        return None

    if response.status_code != 200:
        return None

    try:
        data = response.json()
    except ValueError:
        return None

    session = data.get("session") if isinstance(data, dict) else None
    if not isinstance(session, dict):
        return None

    session_id = session.get("id")
    return session_id if isinstance(session_id, str) and session_id else None
