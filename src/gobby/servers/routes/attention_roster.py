"""Serialize attention roster rows into the entries the roster route serves."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.sessions.turn_lifecycle import TurnLifecycleState
from gobby.storage.attention import (
    AttentionRosterRow,
    AttentionRosterSnapshot,
    AttentionRosterTerminal,
    AttentionState,
)
from gobby.storage.terminals import attach_locator_for_terminal

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def _load_roster_entries(
    server: HTTPServer,
    snapshot: AttentionRosterSnapshot,
    rows: Sequence[AttentionRosterRow],
    display_names: Mapping[tuple[str | None, str | None], str | None],
) -> list[dict[str, object]]:
    """Join cursor-bounded attention with one bounded identity query."""
    runs = [row for row in rows if row.kind == "run"]
    sessions = [row for row in rows if row.kind == "session"]
    attention = {state.entry_id: state for state in snapshot.states}
    entries: list[dict[str, object]] = []
    active_agent_sessions = {run.session_id for run in runs if run.session_id is not None}

    for run in runs:
        entry_id = f"run:{run.source_id}"
        session_attention = (
            attention.get(f"session:{run.session_id}") if run.session_id is not None else None
        )
        entries.append(
            {
                "entry_id": entry_id,
                "run_id": run.source_id,
                "session_id": run.session_id,
                "lifecycle_status": run.lifecycle_status,
                "attention": _serialize_attention(attention.get(entry_id))
                or _serialize_attention(session_attention),
                "provider_error": _provider_error(session_attention),
                "task": _task_payload(run),
                "provider": run.provider,
                "model": run.model,
                "model_display_name": display_names.get((run.provider, run.model)),
                "terminal": _terminal_block(server, run.terminal),
                # Agent runs are native-only; tmux blocks describe external panes.
                "tmux": None,
                "last_activity_at": _serialize_timestamp(run.updated_at),
                **_metadata_payload(snapshot, entry_id),
            }
        )

    for session in sessions:
        if session.source_id in active_agent_sessions:
            continue
        terminal_context = session.terminal_context
        terminal = _terminal_block(server, session.terminal)
        pane = terminal_context.get("tmux_pane")
        if terminal is None and (not isinstance(pane, str) or not pane):
            continue
        entry_id = f"session:{session.source_id}"
        entries.append(
            {
                "entry_id": entry_id,
                "run_id": None,
                "session_id": session.session_id,
                "lifecycle_status": session.lifecycle_status,
                "attention": _serialize_attention(attention.get(entry_id)),
                "provider_error": _provider_error(attention.get(entry_id)),
                "task": _task_payload(session),
                "provider": session.provider,
                "model": session.model,
                "model_display_name": display_names.get((session.provider, session.model)),
                "terminal": terminal,
                "tmux": _session_tmux_payload(terminal_context),
                "last_activity_at": _serialize_timestamp(session.updated_at),
                **_metadata_payload(snapshot, entry_id),
            }
        )
    return sorted(entries, key=lambda item: str(item["entry_id"]))


def _task_payload(row: AttentionRosterRow) -> dict[str, object] | None:
    """A run's task, or the task an interactive session holds open, when it has one."""
    if row.task_id is None or row.task_ref is None:
        return None
    return {
        "id": row.task_id,
        "ref": row.task_ref,
        "stage": row.task_stage,
        "title": row.task_title,
    }


def _model_display_names(
    resolver: Any,
    rows: Sequence[AttentionRosterRow],
) -> dict[tuple[str | None, str | None], str | None]:
    """Each distinct model's name as its provider prints it, when the catalog has it."""
    names: dict[tuple[str | None, str | None], str | None] = {}
    for row in rows:
        key = (row.provider, row.model)
        if not row.provider or not row.model or key in names:
            continue
        capability = resolver.find_model(row.provider, row.model)
        names[key] = None if capability is None else capability.display_name
    return names


def _serialize_attention(state: AttentionState | None) -> dict[str, object] | None:
    if state is None or state.state is None:
        return None
    return {
        "attention_id": state.attention_id,
        "state": state.state,
        "reason": state.reason,
        "kind": state.kind,
        "fingerprint": state.fingerprint,
        "payload": state.payload,
        "since": state.since,
        "seen_at": state.seen_at,
    }


def _provider_error(state: AttentionState | None) -> dict[str, object] | None:
    if state is None:
        return None
    failure = TurnLifecycleState.from_payload(state.payload).provider_error
    return failure.to_dict() if failure is not None else None


def _terminal_block(
    server: HTTPServer,
    terminal: AttentionRosterTerminal | None,
) -> dict[str, object] | None:
    if terminal is None:
        return None
    manager = getattr(server.services, "terminal_manager", None)
    if manager is None:
        return None
    try:
        attach = attach_locator_for_terminal(
            terminal,
            live_host_epoch=terminal.host_epoch or "",
            socket_dir=Path.home() / ".gobby",
        )
    except Exception:
        attach = None
    return {
        "terminal_id": terminal.id,
        "backend": terminal.backend,
        "state": terminal.state,
        "attach": None if attach is None else asdict(attach),
    }


def _session_tmux_payload(terminal_context: Mapping[str, object]) -> dict[str, object]:
    from gobby.terminals.lookup import (
        attach_name_from_context,
        parent_pid_from_context,
        socket_path_from_context,
    )

    return {
        "socket_path": socket_path_from_context(terminal_context),
        "session_name": attach_name_from_context(terminal_context),
        "parent_pid": parent_pid_from_context(terminal_context),
    }


def _metadata_payload(
    snapshot: AttentionRosterSnapshot,
    entry_id: str,
) -> dict[str, object]:
    metadata = snapshot.metadata.get(entry_id)
    return {"metadata": dict(metadata)} if metadata is not None else {}


def _serialize_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
