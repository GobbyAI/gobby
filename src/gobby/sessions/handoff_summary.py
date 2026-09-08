"""Deterministic archival summaries built from delivered clear handoffs."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.hooks.tool_error_tracker import load_open_tool_errors
from gobby.sessions.analyzer import TranscriptAnalyzer
from gobby.sessions.analyzer_turns import (
    SUMMARY_ANALYZER_MAX_RECORDS,
    analyzer_turns_from_transcript,
)
from gobby.sessions.handoff_records import DeliveredHandoff, handoff_summary_source_hash
from gobby.sessions.summary_formatting import format_unresolved_errors
from gobby.sessions.summary_transcripts import _read_transcript_window
from gobby.sessions.transcripts import get_parser
from gobby.sessions.workspace_context import resolve_session_workspace
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.daemon_git import GitOk, daemon_git, parse_porcelain_v1_z
from gobby.workflows.task_claim_state import normalize_task_edited_path

_TERMINAL_TASK_ACTIONS = frozenset({"closed", "escalated", "needs_review", "review_approved"})
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    active_task_id: str | None
    active_task_line: str | None
    commit_shas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HandoffSummary:
    markdown: str
    source_hash: str
    context_summary: dict[str, Any]


async def build_handoff_summary(
    *,
    session: Any,
    handoff: DeliveredHandoff,
    db: HubDatabase,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> HandoffSummary:
    """Append bounded repository evidence to an authored handoff."""
    try:
        task_evidence = await _run_db(
            run_db,
            _load_task_evidence,
            db,
            handoff.session_id,
            handoff.authored_at,
        )
    except Exception:
        task_evidence = TaskEvidence(None, None, ())

    try:
        transcript_commits = await _load_transcript_commit_shas(session)
    except Exception:
        transcript_commits = ()
    commit_shas = _ordered_unique((*task_evidence.commit_shas, *transcript_commits))

    try:
        file_paths = await _run_db(run_db, _load_session_file_paths, db, handoff.session_id)
    except Exception:
        file_paths = ()

    try:
        file_statuses = await _load_file_statuses(session, file_paths)
    except Exception:
        file_statuses = tuple((path, "unknown") for path in file_paths)

    try:
        open_errors = await _run_db(run_db, load_open_tool_errors, db, handoff.session_id)
    except Exception:
        open_errors = []

    sections = (
        _render_section("Active Task", (task_evidence.active_task_line,)),
        _render_section("Commits", tuple(f"`{sha}`" for sha in commit_shas)),
        _render_section(
            "Files Changed",
            tuple(f"`{status}` `{path}`" for path, status in file_statuses),
        ),
        _render_error_section(open_errors),
    )
    markdown = handoff.payload.rendered_markdown + "\n\n" + "\n\n".join(sections)
    return HandoffSummary(
        markdown=markdown,
        source_hash=handoff_summary_source_hash(handoff),
        context_summary={
            "has_active_task": task_evidence.active_task_id is not None,
            "files_modified_count": len(file_paths),
            "git_commits_count": len(commit_shas),
            "has_initial_goal": False,
        },
    )


def find_current_handoff_summary(
    db: HubDatabase,
    session_id: str,
    source_hash: str,
    expected_markdown: str,
) -> str | None:
    """Return the matching valid current summary for an immutable handoff source."""
    row = db.fetchone(
        """
        SELECT summary_markdown, summary_generation_mode, summary_source_context_hash
        FROM sessions
        WHERE id = %s
        """,
        (session_id,),
    )
    from gobby.sessions.summary_validity import is_summary_markdown_valid

    if row is None:
        return None
    if (
        row["summary_source_context_hash"] != source_hash
        or row["summary_generation_mode"] != "agent_authored"
        or row["summary_markdown"] != expected_markdown
        or not is_summary_markdown_valid(expected_markdown)
    ):
        return None
    return expected_markdown


def _load_task_evidence(
    db: HubDatabase,
    session_id: str,
    authored_at: Any,
) -> TaskEvidence:
    rows = db.fetchall(
        """
        SELECT
            st.id AS link_id,
            st.task_id,
            st.action,
            st.created_at,
            t.seq_num,
            t.title,
            t.commits
        FROM session_tasks AS st
        JOIN tasks AS t ON t.id = st.task_id
        WHERE st.session_id = %s
          AND st.created_at <= %s
        ORDER BY st.created_at, st.id
        """,
        (session_id, authored_at),
    )
    actions: dict[str, list[str]] = {}
    task_details: dict[str, tuple[int | None, str]] = {}
    latest_claim: tuple[Any, int, str] | None = None
    commit_shas: list[str] = []
    for row in rows:
        task_id = str(row["task_id"])
        action = str(row["action"])
        actions.setdefault(task_id, []).append(action)
        seq_num = row["seq_num"] if isinstance(row["seq_num"], int) else None
        task_details[task_id] = (seq_num, str(row["title"]))
        if action == "claimed":
            latest_claim = (row["created_at"], int(row["link_id"]), task_id)
        commit_shas.extend(_decode_commit_shas(row["commits"]))

    active_task_id: str | None = None
    if latest_claim is not None:
        claimed_candidates = [
            (row["created_at"], int(row["link_id"]), str(row["task_id"]))
            for row in rows
            if row["action"] == "claimed"
            and not any(
                action in _TERMINAL_TASK_ACTIONS for action in actions.get(str(row["task_id"]), ())
            )
        ]
        if claimed_candidates:
            active_task_id = max(claimed_candidates)[2]

    active_line: str | None = None
    if active_task_id is not None:
        seq_num, title = task_details[active_task_id]
        ref = f"#{seq_num}" if seq_num is not None else active_task_id[:8]
        active_line = f"{ref} [in_progress] {title}"
    return TaskEvidence(active_task_id, active_line, _ordered_unique(commit_shas))


def _load_session_file_paths(db: HubDatabase, session_id: str) -> tuple[str, ...]:
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    if row is None:
        return ()
    raw = row["variables"]
    variables = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(variables, Mapping):
        raise ValueError("session variables are not an object")
    values = variables.get("session_edited_files", ())
    if not isinstance(values, list):
        raise ValueError("session_edited_files is not an array")
    normalized = (normalize_task_edited_path(value) for value in values)
    return _ordered_unique(path for path in normalized if path is not None)


async def _load_transcript_commit_shas(session: Any) -> tuple[str, ...]:
    transcript_path = getattr(session, "transcript_path", None)
    path = Path(transcript_path) if isinstance(transcript_path, str) else None
    if path is None or not path.exists():
        raise FileNotFoundError("session transcript is unavailable")
    source = getattr(session, "source", None)
    window = await _read_transcript_window(
        path,
        source=source or "",
        max_records=SUMMARY_ANALYZER_MAX_RECORDS,
    )
    parser_source = _parser_source(source, window.turns)
    parser = get_parser(
        parser_source,
        session_id=getattr(session, "id", None),
        transcript_path=path,
    )
    turns = (
        window.turns
        if parser_source == "claude"
        else await asyncio.to_thread(analyzer_turns_from_transcript, parser, window.turns)
    )
    context = await asyncio.to_thread(
        TranscriptAnalyzer(parser).extract_handoff_context,
        turns,
    )
    return _ordered_unique(
        commit["hash"]
        for commit in context.git_commits
        if isinstance(commit, Mapping)
        and isinstance(commit.get("hash"), str)
        and _COMMIT_SHA.fullmatch(commit["hash"])
    )


async def _load_file_statuses(
    session: Any,
    paths: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    if not paths:
        return ()
    cwd = resolve_session_workspace(session, getattr(session, "transcript_path", None))
    result = await daemon_git.status(cwd, paths, timeout=5.0)
    if not isinstance(result, GitOk):
        detail = result.stderr.strip() or result.status
        raise RuntimeError(f"Git status unavailable: {detail}")
    statuses: dict[str, str] = {}
    for entry in parse_porcelain_v1_z(result.stdout):
        status = entry.code.strip() or "changed"
        for path in (entry.path, entry.original_path):
            if path is not None and path in paths:
                statuses[path] = status
    return tuple((path, statuses.get(path, "clean")) for path in paths)


def _decode_commit_shas(raw: Any) -> tuple[str, ...]:
    values = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(values, list):
        return ()
    return tuple(
        value for value in values if isinstance(value, str) and _COMMIT_SHA.fullmatch(value)
    )


def _parser_source(source: str | None, turns: list[dict[str, Any]]) -> str:
    if source == "unknown" and any(
        isinstance(turn.get("content"), (str, list))
        and isinstance(turn.get("type"), str)
        and "message" not in turn
        for turn in turns
    ):
        return "qwen"
    if not source:
        raise ValueError("unsupported transcript source")
    return source


async def _run_db(
    runner: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
) -> Any:
    if runner is not None:
        return await runner(func, *args)
    return await asyncio.to_thread(func, *args)


def _render_section(title: str, values: tuple[str | None, ...]) -> str:
    lines = [f"- {value}" for value in values if value]
    return f"## {title}\n\n" + ("\n".join(lines) if lines else "None.")


def _render_error_section(records: list[dict[str, Any]]) -> str:
    rendered = format_unresolved_errors(records)
    prefix = "Unresolved Tool Errors:\n"
    body = rendered.removeprefix(prefix) if rendered else "None."
    return f"## Unresolved Errors\n\n{body}"


def _ordered_unique(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))
