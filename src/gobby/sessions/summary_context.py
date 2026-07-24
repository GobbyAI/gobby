"""Prompt context helpers for session summary generation."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.hooks.tool_error_tracker import load_open_tool_errors
from gobby.sessions.summary_transcripts import (
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    _digest_markdown_for_summary,
    _extract_digest_turns,
    _format_transcript_fallback_summary,
    _strip_injected_context_from_value,
    _summary_source_text,
    _truncate_markdown,
)
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.sessions.summarize import SessionManagerProtocol, SessionSummaryConfigProtocol

logger = logging.getLogger("gobby.sessions.summarize")


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
    from gobby.workflows.git_utils import get_file_changes, get_git_diff_summary
    from gobby.workflows.summary_actions import (
        _format_structured_context,
        format_turns_for_llm,
    )

    digest_markdown = _digest_markdown_for_summary(session)
    source = getattr(session, "source", None) or "claude"
    if source == "unknown" and any(
        isinstance(turn.get("content"), (str, list))
        and isinstance(turn.get("type"), str)
        and "message" not in turn
        for turn in turns
    ):
        source = "qwen"
    first_digest_turn, recent_digest_turns = _extract_digest_turns(digest_markdown)
    if digest_markdown:
        transcript_summary = _truncate_markdown(
            digest_markdown,
            TRANSCRIPT_FALLBACK_MAX_CHARS,
        )
        last_messages_str = recent_digest_turns
    else:
        parser: Any
        if source == "qwen":
            from gobby.sessions.transcripts.qwen import QwenTranscriptParser

            parser = QwenTranscriptParser(session_id=getattr(session, "id", None))
        elif source == "grok":
            from gobby.sessions.transcripts.grok import GrokTranscriptParser

            parser = GrokTranscriptParser()
        elif source == "codex":
            from gobby.sessions.transcripts.codex import CodexTranscriptParser

            parser = CodexTranscriptParser()
        elif source == "droid":
            from gobby.sessions.transcripts.droid import DroidTranscriptParser

            parser = DroidTranscriptParser(
                session_id=getattr(session, "id", None),
                transcript_path=getattr(session, "transcript_path", None),
            )
        else:
            from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

            parser = ClaudeTranscriptParser()

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

    return {
        "transcript_summary": transcript_summary,
        "last_messages": last_messages_str,
        "git_status": handoff_ctx.git_status or "",
        "file_changes": get_file_changes(project_path=project_path),
        "git_diff_summary": get_git_diff_summary(project_path=project_path),
        "structured_context": _format_structured_context(handoff_ctx),
        "claimed_tasks": claimed_tasks,
        "session_memories": session_memories,
        "first_digest_turn": first_digest_turn,
        "recent_digest_turns": recent_digest_turns,
        "external_id": session.id[:12],
        "session_id": session.id,
        "session_source": source,
        "project_path": project_path,
    }


def _source_hash_payload(
    *,
    session: Any,
    digest_markdown: str,
    summary_context: dict[str, Any],
    prompt_template: str | None,
) -> dict[str, Any]:
    return {
        "digest_markdown": digest_markdown,
        "last_turn_markdown": _summary_source_text(getattr(session, "last_turn_markdown", None)),
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
