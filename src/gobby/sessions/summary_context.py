"""Prompt context helpers for session summary generation."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Awaitable, Callable
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks.tool_error_tracker import load_open_tool_errors
from gobby.sessions.summary_transcripts import (
    _format_transcript_fallback_summary,
    _strip_injected_context_from_value,
    _summary_source_text,
)
from gobby.sessions.workspace_context import _session_git_paths
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.sessions.summarize import SessionManagerProtocol, SessionSummaryConfigProtocol

logger = logging.getLogger("gobby.sessions.summarize")


def _decode_git_status_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
        if isinstance(decoded, str):
            return decoded
    return value


def _git_status_paths(line: str) -> tuple[str, ...]:
    payload = line[3:] if len(line) >= 3 else line
    if " -> " not in payload:
        return (_decode_git_status_path(payload),)
    source, destination = payload.split(" -> ", 1)
    return (
        _decode_git_status_path(source),
        _decode_git_status_path(destination),
    )


def _scoped_git_status(status: str, paths: tuple[str, ...]) -> str:
    requested_paths = set(paths)
    scoped: list[str] = []
    for line in status.splitlines():
        if requested_paths.intersection(_git_status_paths(line)):
            scoped.append(line)
    return "\n".join(scoped)


def _facade_attr(name: str) -> Any:
    from gobby.sessions import summarize

    return getattr(summarize, name)


def _looks_like_mock(value: Any) -> bool:
    return type(value).__module__.startswith("unittest.mock")


def _summary_context_db(
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
) -> HubDatabase | None:
    if db is not None:
        return db
    resolved_db = getattr(session_manager, "db", None)
    return None if _looks_like_mock(resolved_db) else resolved_db


def load_summary_prompt_template(
    *,
    path: str,
    session_summary_config: SessionSummaryConfigProtocol | None,
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
) -> str | None:
    prompt_template = getattr(session_summary_config, "prompt", None)
    resolved_db = _summary_context_db(db, session_manager)
    if resolved_db is None:
        return prompt_template

    try:
        from gobby.prompts.loader import PromptLoader

        loader = PromptLoader(db=resolved_db)
        prompt_obj = loader.load(path)
        prompt_template = prompt_obj.content
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(
            "Failed to load summary prompt",
            extra={"path": str(path), "error": str(e)},
        )

    return prompt_template


async def _build_summary_prompt_context(
    *,
    session: Any,
    turns: list[dict[str, Any]],
    handoff_ctx: Any,
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    from gobby.sessions.summary_formatting import (
        _format_structured_context,
        format_turns_for_llm,
    )
    from gobby.sessions.transcripts import get_parser
    from gobby.workflows.git_utils import get_file_changes, get_git_diff_summary

    source = getattr(session, "source", None)
    parser = get_parser(
        source,
        session_id=getattr(session, "id", None),
        transcript_path=getattr(session, "transcript_path", None),
    )

    last_turns = _strip_injected_context_from_value(parser.extract_turns_since_clear(turns))
    transcript_summary = _format_transcript_fallback_summary(
        last_turns,
        format_turns_for_llm,
    )
    last_messages = _strip_injected_context_from_value(
        parser.extract_last_messages(turns, num_pairs=2)
    )
    last_messages_str = format_turns_for_llm(last_messages) if last_messages else ""

    resolved_db = _summary_context_db(db, session_manager)
    run_db_fn = _facade_attr("_run_db")
    handoff_ctx.unresolved_errors = (
        await run_db_fn(run_db, load_open_tool_errors, resolved_db, session.id)
        if resolved_db
        else []
    )
    claimed_tasks = (
        await run_db_fn(run_db, _get_claimed_tasks, session.id, resolved_db) if resolved_db else ""
    )
    session_memories = (
        await run_db_fn(run_db, _get_session_memories, session.id, resolved_db)
        if resolved_db
        else ""
    )
    has_session_edits = bool(handoff_ctx.files_modified)
    project_root = Path(project_path).resolve() if project_path else Path.cwd().resolve()
    session_paths = _session_git_paths(
        [str(path) for path in handoff_ctx.files_modified],
        project_root,
    )
    structured_handoff_ctx = copy(handoff_ctx)
    if has_session_edits:
        structured_handoff_ctx.git_status = _scoped_git_status(
            handoff_ctx.git_status,
            session_paths,
        )
    else:
        structured_handoff_ctx.git_status = ""
        structured_handoff_ctx.git_commits = []

    file_changes = (
        await run_db_fn(
            run_db,
            get_file_changes,
            project_path=project_path,
            paths=session_paths,
        )
        if has_session_edits
        else ""
    )
    git_diff_summary = (
        await run_db_fn(
            run_db,
            get_git_diff_summary,
            project_path=project_path,
            paths=session_paths,
        )
        if has_session_edits
        else ""
    )

    return {
        "transcript_summary": transcript_summary,
        "last_messages": last_messages_str,
        "git_status": structured_handoff_ctx.git_status,
        "file_changes": file_changes,
        "git_diff_summary": git_diff_summary,
        "structured_context": _format_structured_context(structured_handoff_ctx),
        "claimed_tasks": claimed_tasks,
        "session_memories": session_memories,
        "external_id": session.id[:12],
        "session_id": session.id,
        "session_source": source,
        "project_path": project_path,
    }


def _source_hash_payload(
    *,
    session: Any,
    summary_context: dict[str, Any],
    prompt_template: str | None,
) -> dict[str, Any]:
    return {
        "last_assistant_content": _summary_source_text(
            getattr(session, "last_assistant_content", None)
        ),
        "prompt_template": prompt_template or "",
        "summary_context": summary_context,
    }


def _get_claimed_tasks(session_id: str, db: HubDatabase) -> str:
    """Get tasks assigned to this session, formatted for LLM context.

    Args:
        session_id: Platform session UUID.
        db: Database instance.

    Returns:
        Formatted string with task refs, titles, states, and dependencies.
    """
    try:
        from gobby.storage.session_tasks import SessionTaskManager

        stm = SessionTaskManager(db)
        task_rows: list[dict[str, Any]] = stm.get_session_tasks(session_id)
        if not task_rows:
            return ""

        from gobby.storage.task_dependencies import TaskDependencyManager
        from gobby.tasks.state_semantics import projected_task_state

        dep_mgr = TaskDependencyManager(db)
        lines: list[str] = []
        for row in task_rows:
            task = row["task"]
            ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
            state = projected_task_state(task)
            title = task.title
            desc_snippet = ""
            if task.description:
                desc_snippet = task.description[:120].replace("\n", " ")
                if len(task.description) > 120:
                    desc_snippet += "..."

            line = f"- {ref} [{state}] {title}"
            if desc_snippet:
                line += f"\n  {desc_snippet}"

            # Include blocking dependencies
            try:
                deps = dep_mgr.get_all_dependencies(task.id)
                blockers = [d for d in deps if d.dep_type == "blocks"]
                if blockers:
                    blocker_ids = ", ".join(d.depends_on[:8] for d in blockers)
                    line += f"\n  Blocked by: {blocker_ids}"
            except Exception as e:
                logger.debug(
                    "Failed to get dependencies for claimed task",
                    extra={"task_id": task.id, "error": str(e)},
                )

            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.debug(
            "Failed to get claimed tasks for session",
            extra={"session_id": session_id, "error": str(e)},
        )
        return ""


def _get_session_memories(session_id: str, db: HubDatabase) -> str:
    """Get memories stored during this session, formatted for LLM context.

    Args:
        session_id: Platform session UUID.
        db: Database instance.

    Returns:
        Formatted string with memory content snippets and tags.
    """
    try:
        rows = db.fetchall(
            """SELECT content, tags, memory_type
            FROM memories
            WHERE source_session_id = %s
            ORDER BY created_at DESC
            LIMIT 20""",
            (session_id,),
        )
        if not rows:
            return ""

        lines: list[str] = []
        for row in rows:
            content = str(row["content"]).strip()
            if len(content) > 200:
                content = content[:197] + "..."
            tags = row["tags"] or ""
            if tags:
                try:
                    tag_list = json.loads(tags)
                    if isinstance(tag_list, list):
                        tags = ", ".join(tag_list)
                except json.JSONDecodeError:
                    pass
            mem_type = row["memory_type"] or "fact"
            line = f"- [{mem_type}] {content}"
            if tags:
                line += f" (tags: {tags})"
            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.debug(
            "Failed to get session memories",
            extra={"session_id": session_id, "error": str(e)},
        )
        return ""
