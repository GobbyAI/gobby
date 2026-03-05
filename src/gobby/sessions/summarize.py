"""Shared session summary generation.

Single entry point for producing compact_markdown and summary_markdown
at session boundaries. Used by:
- MCP set_handoff_context (automated fallback path)
- hook_manager._dispatch_session_summaries (graceful exit via /clear, /exit, /compact)
- SessionLifecycleManager (expired sessions safety net)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Protocol

import aiofiles

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

TURN_PATTERN = re.compile(r"^### Turn \d+", re.MULTILINE)


class SessionManagerProtocol(Protocol):
    def get(self, session_id: str) -> Any: ...
    def update_compact_markdown(self, session_id: str, compact_markdown: str) -> Any: ...
    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = ...,
        summary_markdown: str | None = ...,
    ) -> Any: ...
    def update_status(self, session_id: str, status: str) -> Any: ...


class LLMServiceProtocol(Protocol):
    def get_default_provider(self) -> Any: ...


async def generate_session_summaries(
    session_id: str,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None = None,
    db: DatabaseProtocol | None = None,
    write_file: bool = False,
    output_path: str = "~/.gobby/session_summaries",
    set_handoff_ready: bool = True,
    compact_only: bool = False,
    full_only: bool = False,
) -> dict[str, Any]:
    """Generate compact_markdown and summary_markdown for a session.

    Reads the transcript, runs TranscriptAnalyzer for compact context,
    uses LLM for full archival summary, persists to DB, and optionally
    writes files to session_summaries directory.

    Args:
        session_id: Platform session ID (UUID).
        session_manager: LocalSessionManager instance.
        llm_service: LLM service for generating full summaries.
        db: Database for prompt template loading.
        write_file: Write summary files to disk.
        output_path: Directory for summary files.
        set_handoff_ready: Update session status to handoff_ready.
        compact_only: Generate compact summary only (TranscriptAnalyzer).
        full_only: Generate full LLM summary only.

    Returns:
        Dict with success status, markdown lengths, and context summary.
    """
    if not session_manager:
        return {"success": False, "error": "Session manager not available"}

    session = await asyncio.to_thread(session_manager.get, session_id)
    if not session:
        return {"success": False, "error": "No session found", "session_id": session_id}

    # Get transcript path
    transcript_path = session.jsonl_path
    if not transcript_path:
        return {
            "success": False,
            "error": "No transcript path for session",
            "session_id": session_id,
        }

    path = Path(transcript_path)
    if not path.exists():
        return {
            "success": False,
            "error": "Transcript file not found",
            "path": transcript_path,
        }

    # Read and parse transcript
    turns = await _read_transcript(path)

    # Analyze transcript
    from gobby.sessions.analyzer import TranscriptAnalyzer

    analyzer = TranscriptAnalyzer()
    handoff_ctx = analyzer.extract_handoff_context(turns)

    # Enrich with real-time git status
    await _enrich_git_context(handoff_ctx, path.parent)

    # Determine what to generate (neither flag = both)
    generate_compact = compact_only or not full_only
    generate_full = full_only or not compact_only

    compact_markdown: str | None = None
    full_markdown: str | None = None
    full_error: str | None = None

    if generate_compact:
        compact_markdown, compact_error = await _generate_compact_summary(
            session=session,
            turns=turns,
            handoff_ctx=handoff_ctx,
            llm_service=llm_service,
            db=db,
            session_manager=session_manager,
        )
        if not compact_markdown:
            # Fallback to code-only renderer when LLM is unavailable
            logger.warning(
                "Compact LLM summary failed (%s), falling back to code-only",
                compact_error,
            )
            from gobby.sessions.formatting import format_handoff_as_markdown

            compact_markdown = format_handoff_as_markdown(handoff_ctx)

    if generate_full:
        full_markdown, full_error = await _generate_full_summary(
            session=session,
            turns=turns,
            handoff_ctx=handoff_ctx,
            llm_service=llm_service,
            db=db,
            session_manager=session_manager,
        )
        if full_error and full_only and not compact_only:
            return {
                "success": False,
                "error": f"Failed to generate full summary: {full_error}",
                "session_id": session_id,
            }

    # Persist to database
    if compact_markdown:
        await asyncio.to_thread(
            session_manager.update_compact_markdown, session_id, compact_markdown
        )
    if full_markdown:
        await asyncio.to_thread(
            session_manager.update_summary, session_id, summary_markdown=full_markdown
        )

    # Set handoff_ready status
    if set_handoff_ready:
        await asyncio.to_thread(session_manager.update_status, session_id, "handoff_ready")

    # Write files if requested
    files_written = await _write_files(
        session_id=session_id,
        full_markdown=full_markdown,
        compact_markdown=compact_markdown,
        write_file=write_file,
        output_path=output_path,
        session_manager=session_manager,
    )

    logger.info(
        "Session summaries generated for %s (compact=%d, full=%d chars)",
        session_id,
        len(compact_markdown) if compact_markdown else 0,
        len(full_markdown) if full_markdown else 0,
    )

    return {
        "success": True,
        "session_id": session_id,
        "compact_length": len(compact_markdown) if compact_markdown else 0,
        "full_length": len(full_markdown) if full_markdown else 0,
        "full_error": full_error,
        "files_written": files_written,
        "context_summary": {
            "has_active_task": bool(handoff_ctx.active_gobby_task),
            "files_modified_count": len(handoff_ctx.files_modified),
            "git_commits_count": len(handoff_ctx.git_commits),
            "has_initial_goal": bool(handoff_ctx.initial_goal),
        },
    }


async def _read_transcript(path: Path) -> list[dict[str, Any]]:
    """Read and parse JSONL transcript file."""
    turns: list[dict[str, Any]] = []
    async with aiofiles.open(path, encoding="utf-8") as f:
        async for idx, line in async_enumerate(f):
            if line.strip():
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line %d in %s", idx + 1, path)
    return turns


async def async_enumerate(aiter: Any, start: int = 0) -> Any:
    """Async version of enumerate."""
    idx = start
    async for item in aiter:
        yield idx, item
        idx += 1


async def _enrich_git_context(handoff_ctx: Any, cwd: Path) -> None:
    """Enrich HandoffContext with real-time git status and commits."""
    if not handoff_ctx.git_status:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            handoff_ctx.git_status = stdout.decode().strip() if proc.returncode == 0 else ""
        except Exception as e:
            logger.debug("Failed to get git status for %s: %s", cwd, e)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "--oneline",
            "-10",
            "--format=%H|%s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            commits = []
            for line in stdout.decode().strip().split("\n"):
                if "|" in line:
                    hash_val, message = line.split("|", 1)
                    commits.append({"hash": hash_val, "message": message})
            if commits:
                handoff_ctx.git_commits = commits
    except Exception as e:
        logger.debug("Failed to get git log for %s: %s", cwd, e)


def _resolve_provider(llm_service: LLMServiceProtocol | None) -> Any:
    """Resolve LLM provider from service or fallback to ClaudeLLMProvider."""
    provider = llm_service.get_default_provider() if llm_service else None
    if not provider:
        from gobby.config.app import load_config
        from gobby.llm.claude import ClaudeLLMProvider

        config = load_config()
        provider = ClaudeLLMProvider(config)
    return provider


async def _generate_full_summary(
    session: Any,
    turns: list[dict[str, Any]],
    handoff_ctx: Any,
    llm_service: LLMServiceProtocol | None,
    db: DatabaseProtocol | None,
    session_manager: SessionManagerProtocol,
) -> tuple[str | None, str | None]:
    """Generate the full LLM-based archival summary.

    Returns:
        Tuple of (full_markdown, error_message). One will be None.
    """
    try:
        provider = _resolve_provider(llm_service)

        # Get transcript parser
        from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

        parser = ClaudeTranscriptParser()

        # Load prompt template
        prompt_template = None
        try:
            from gobby.prompts.loader import PromptLoader

            loader = PromptLoader(db=db or getattr(session_manager, "db", None))
            prompt_obj = loader.load("handoff/session_end")
            prompt_template = prompt_obj.content
        except FileNotFoundError:
            pass

        if not prompt_template:
            return None, "Missing prompt template: handoff/session_end"

        # Prepare context for LLM
        from gobby.workflows.git_utils import get_file_changes, get_git_diff_summary
        from gobby.workflows.summary_actions import (
            _format_structured_context,
            format_turns_for_llm,
        )

        last_turns = parser.extract_turns_since_clear(turns, max_turns=50)
        last_messages = parser.extract_last_messages(turns, num_pairs=2)
        last_messages_str = format_turns_for_llm(last_messages) if last_messages else ""

        file_changes = get_file_changes()
        git_diff_summary = get_git_diff_summary()
        structured_context = _format_structured_context(handoff_ctx)

        # Enrich with DB context (same as compact path)
        resolved_db = db or getattr(session_manager, "db", None)
        claimed_tasks = (
            await asyncio.to_thread(_get_claimed_tasks, session.id, resolved_db)
            if resolved_db
            else ""
        )
        session_memories = (
            await asyncio.to_thread(_get_session_memories, session.id, resolved_db)
            if resolved_db
            else ""
        )
        first_digest_turn, recent_digest_turns = _extract_digest_turns(session.digest_markdown)

        context = {
            "transcript_summary": format_turns_for_llm(last_turns),
            "last_messages": last_messages_str,
            "git_status": handoff_ctx.git_status or "",
            "file_changes": file_changes,
            "git_diff_summary": git_diff_summary,
            "structured_context": structured_context,
            "claimed_tasks": claimed_tasks,
            "session_memories": session_memories,
            "first_digest_turn": first_digest_turn,
            "recent_digest_turns": recent_digest_turns,
            "external_id": session.id[:12],
            "session_id": session.id,
            "session_source": session.source,
        }

        full_markdown = await provider.generate_summary(context, prompt_template=prompt_template)
        return full_markdown, None

    except Exception as e:
        logger.error(
            "Failed to generate full summary for session %s: %s",
            session.id,
            e,
            exc_info=True,
        )
        return None, str(e)


async def _generate_compact_summary(
    session: Any,
    turns: list[dict[str, Any]],
    handoff_ctx: Any,
    llm_service: LLMServiceProtocol | None,
    db: DatabaseProtocol | None,
    session_manager: SessionManagerProtocol,
) -> tuple[str | None, str | None]:
    """Generate LLM-based compact handoff summary using handoff/compact prompt.

    Returns:
        Tuple of (compact_markdown, error_message). One will be None.
    """
    try:
        provider = _resolve_provider(llm_service)

        # Get transcript parser
        from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

        parser = ClaudeTranscriptParser()

        # Load compact prompt template
        prompt_template = None
        try:
            from gobby.prompts.loader import PromptLoader

            loader = PromptLoader(db=db or getattr(session_manager, "db", None))
            prompt_obj = loader.load("handoff/compact")
            prompt_template = prompt_obj.content
        except FileNotFoundError:
            pass

        if not prompt_template:
            return None, "No prompt template found for handoff/compact"

        # Prepare context for LLM
        from gobby.workflows.git_utils import get_file_changes, get_git_diff_summary
        from gobby.workflows.summary_actions import (
            _format_structured_context,
            format_turns_for_llm,
        )

        last_turns = parser.extract_turns_since_clear(turns, max_turns=50)
        last_messages = parser.extract_last_messages(turns, num_pairs=2)
        last_messages_str = format_turns_for_llm(last_messages) if last_messages else ""

        file_changes = get_file_changes()
        git_diff_summary = get_git_diff_summary()
        structured_context = _format_structured_context(handoff_ctx)

        # Enrich with DB context
        resolved_db = db or getattr(session_manager, "db", None)
        claimed_tasks = (
            await asyncio.to_thread(_get_claimed_tasks, session.id, resolved_db)
            if resolved_db
            else ""
        )
        session_memories = (
            await asyncio.to_thread(_get_session_memories, session.id, resolved_db)
            if resolved_db
            else ""
        )
        first_digest_turn, recent_digest_turns = _extract_digest_turns(session.digest_markdown)

        # Get previous compact_markdown for cumulative compression
        previous_summary = session.compact_markdown or ""

        context = {
            "transcript_summary": format_turns_for_llm(last_turns),
            "last_messages": last_messages_str,
            "git_status": handoff_ctx.git_status or "",
            "file_changes": file_changes,
            "git_diff_summary": git_diff_summary,
            "structured_context": structured_context,
            "previous_summary": previous_summary,
            "claimed_tasks": claimed_tasks,
            "session_memories": session_memories,
            "first_digest_turn": first_digest_turn,
            "recent_digest_turns": recent_digest_turns,
        }

        compact_markdown = await provider.generate_summary(context, prompt_template=prompt_template)
        return compact_markdown, None

    except Exception as e:
        logger.error(
            "Failed to generate compact summary for session %s: %s",
            session.id,
            e,
            exc_info=True,
        )
        return None, str(e)


def _get_claimed_tasks(session_id: str, db: DatabaseProtocol) -> str:
    """Get tasks assigned to this session, formatted for LLM context.

    Args:
        session_id: Platform session UUID.
        db: Database instance.

    Returns:
        Formatted string with task refs, titles, statuses, and dependencies.
    """
    try:
        from gobby.storage.session_tasks import SessionTaskManager

        stm = SessionTaskManager(db)
        task_rows: list[dict[str, Any]] = stm.get_session_tasks(session_id)
        if not task_rows:
            return ""

        from gobby.storage.task_dependencies import TaskDependencyManager

        dep_mgr = TaskDependencyManager(db)
        lines: list[str] = []
        for row in task_rows:
            task = row["task"]
            ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
            status = task.status
            title = task.title
            desc_snippet = ""
            if task.description:
                desc_snippet = task.description[:120].replace("\n", " ")
                if len(task.description) > 120:
                    desc_snippet += "..."

            line = f"- {ref} [{status}] {title}"
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
                logger.debug("Failed to get dependencies for task %s: %s", task.id, e)

            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.debug("Failed to get claimed tasks for session %s: %s", session_id, e)
        return ""


def _get_session_memories(session_id: str, db: DatabaseProtocol) -> str:
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
            WHERE source_session_id = ?
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
        logger.debug("Failed to get session memories for %s: %s", session_id, e)
        return ""


def _extract_digest_turns(digest_markdown: str | None) -> tuple[str, str]:
    """Extract first and last digest turns from rolling digest markdown.

    Args:
        digest_markdown: The session's rolling digest_markdown field.

    Returns:
        Tuple of (first_turn_text, recent_turns_text). Empty strings if unavailable.
    """
    if not digest_markdown:
        return "", ""

    # Split on ### Turn N headings
    parts = TURN_PATTERN.split(digest_markdown)
    headings = TURN_PATTERN.findall(digest_markdown)

    if not headings:
        # No turn structure — return first 500 chars as first turn
        return digest_markdown[:500].strip(), ""

    # parts[0] is content before first heading (preamble), parts[1:] are turn contents
    # Pair headings with their content
    turns: list[str] = []
    for i, heading in enumerate(headings):
        content = parts[i + 1] if (i + 1) < len(parts) else ""
        turns.append(f"{heading}\n{content.strip()}")

    first_turn = turns[0] if turns else ""
    # Last 2 turns for recent context
    recent = turns[-2:] if len(turns) >= 2 else turns
    recent_turns = "\n\n".join(recent)

    # Truncate to avoid blowing up the prompt
    if len(first_turn) > 800:
        first_turn = first_turn[:800] + "\n..."
    if len(recent_turns) > 1500:
        recent_turns = recent_turns[:1500] + "\n..."

    return first_turn, recent_turns


async def _write_files(
    session_id: str,
    full_markdown: str | None,
    compact_markdown: str | None,
    write_file: bool,
    output_path: str,
    session_manager: SessionManagerProtocol,
) -> list[str]:
    """Write summary files to disk if requested."""
    files_written: list[str] = []
    if not write_file:
        return files_written

    from gobby.workflows.summary_actions import _write_summary_file

    if full_markdown:
        full_path: str | None = await _write_summary_file(
            session_id, full_markdown, output_path, session_manager, mode="full"
        )
        if full_path:
            files_written.append(full_path)

    if compact_markdown:
        compact_path: str | None = await _write_summary_file(
            session_id, compact_markdown, output_path, session_manager, mode="compact"
        )
        if compact_path:
            files_written.append(compact_path)

    return files_written
