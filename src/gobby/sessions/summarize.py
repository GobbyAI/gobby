"""Shared session summary generation.

Single entry point for producing summary_markdown at session boundaries.
Used by:
- MCP set_handoff_context (automated fallback path)
- hook_manager._dispatch_session_summaries (graceful exit via /clear, /exit, /compact)
- SessionLifecycleManager (expired sessions safety net)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import aiofiles

from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

TURN_PATTERN = re.compile(r"^### Turn \d+", re.MULTILINE)
TRANSCRIPT_FALLBACK_MAX_TURNS = 80
TRANSCRIPT_FALLBACK_MAX_CHARS = 24_000
DIGEST_FALLBACK_MAX_CHARS = 24_000


class SessionManagerProtocol(Protocol):
    def get(self, session_id: str) -> Any: ...
    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = ...,
        summary_markdown: str | None = ...,
    ) -> Any: ...
    def update_status(self, session_id: str, status: str) -> Any: ...


class LLMServiceProtocol(Protocol):
    def get_default_provider(self) -> Any: ...


async def _run_sqlite(
    run_db: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if run_db is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return await run_db(func, *args, **kwargs)


def _resolve_run_db(
    explicit_run_db: Callable[..., Awaitable[Any]] | None,
    *,
    db: DatabaseProtocol | None,
    session_manager: SessionManagerProtocol,
) -> Callable[..., Awaitable[Any]] | None:
    if explicit_run_db is not None:
        return explicit_run_db

    resolved_db = db or getattr(session_manager, "db", None)
    if resolved_db is None:
        return None

    try:
        from gobby.app_context import get_app_context

        app_context = get_app_context()
    except Exception:
        return None

    if (
        app_context is not None
        and getattr(app_context, "database", None) is resolved_db
        and getattr(app_context, "db_executor", None) is not None
    ):
        return app_context.run_db
    return None


async def generate_session_summaries(
    session_id: str,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None = None,
    db: DatabaseProtocol | None = None,
    write_file: bool = False,
    output_path: str = ".gobby/session_summaries",
    set_handoff_ready: bool = True,
    compact_only: bool = False,
    full_only: bool = False,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Generate summary_markdown for a session.

    Reads the transcript, runs TranscriptAnalyzer for context,
    uses LLM for archival summary, persists to DB, and optionally
    writes files to session_summaries directory.

    Args:
        session_id: Platform session ID (UUID).
        session_manager: SessionManager instance.
        llm_service: LLM service for generating summaries.
        db: Database for prompt template loading.
        write_file: Write summary files to disk.
        output_path: Directory for summary files.
        set_handoff_ready: Update session status to handoff_ready.
        compact_only: Ignored (kept for API compatibility).
        full_only: Ignored (kept for API compatibility).
        run_db: Optional bounded executor bridge for SQLite storage calls.

    Returns:
        Dict with success status, markdown lengths, and context summary.
    """
    if not session_manager:
        return {"success": False, "error": "Session manager not available"}

    sqlite_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)

    session = await _run_sqlite(sqlite_runner, session_manager.get, session_id)
    if not session:
        return {"success": False, "error": "No session found", "session_id": session_id}

    digest_markdown = _summary_source_text(getattr(session, "digest_markdown", None))
    transcript_path = getattr(session, "transcript_path", None)
    path = Path(transcript_path) if transcript_path else None
    source = getattr(session, "source", None) or "claude"
    turns: list[dict[str, Any]] = []

    if not digest_markdown:
        if not transcript_path:
            return {
                "success": False,
                "error": "No transcript path for session",
                "session_id": session_id,
            }
        if path is None or not path.exists():
            return {
                "success": False,
                "error": "Transcript file not found",
                "path": transcript_path,
            }

        # Transcript summarization is the fallback for older sessions with no digest.
        turns = await _read_transcript(path, source=source)

    # Analyze transcript
    from gobby.sessions.analyzer import TranscriptAnalyzer

    analyzer = TranscriptAnalyzer()
    handoff_ctx = analyzer.extract_handoff_context(turns)

    # Enrich with real-time git status
    cwd = path.parent if path and path.exists() else Path.cwd()
    await _enrich_git_context(handoff_ctx, cwd)

    # Generate full summary
    full_markdown, full_error = await _generate_full_summary(
        session=session,
        turns=turns,
        handoff_ctx=handoff_ctx,
        llm_service=llm_service,
        db=db,
        session_manager=session_manager,
        run_db=sqlite_runner,
    )

    if not is_summary_markdown_valid(full_markdown):
        # Fallback to code-only renderer when LLM is unavailable
        logger.warning(
            f"Full LLM summary failed ({full_error}), falling back to code-only",
        )
        full_markdown = _format_deterministic_summary(handoff_ctx, digest_markdown)

    # Persist to database
    if is_summary_markdown_valid(full_markdown):
        await _run_sqlite(
            sqlite_runner,
            session_manager.update_summary,
            session_id,
            summary_markdown=full_markdown,
        )

    # Set handoff_ready status
    if set_handoff_ready:
        await _run_sqlite(sqlite_runner, session_manager.update_status, session_id, "handoff_ready")

    # Write files if requested
    files_written = await _write_files(
        session_id=session_id,
        full_markdown=full_markdown,
        write_file=write_file,
        output_path=output_path,
        session_manager=session_manager,
    )

    # Record progressive discovery savings
    resolved_db = db or getattr(session_manager, "db", None)
    if resolved_db and getattr(session, "project_id", None):
        try:
            from gobby.savings.discovery import record_discovery_savings

            await _run_sqlite(
                sqlite_runner,
                record_discovery_savings,
                resolved_db,
                session.id,
                session.project_id,
                getattr(session, "model", None),
            )
        except Exception as e:
            logger.warning(f"Failed to record discovery savings for {session_id}: {e}")

    logger.info(
        f"Session summary generated for {session_id} ({(len(full_markdown) if full_markdown else 0)} chars)",
    )

    return {
        "success": True,
        "session_id": session_id,
        "compact_length": 0,  # Kept for API compatibility
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


async def _read_transcript(path: Path, source: str = "claude") -> list[dict[str, Any]]:
    """Read and parse a transcript file in its native format.

    Claude, Codex, and Droid use JSONL (one JSON object per line).
    Gemini/Qwen store sessions as a single JSON object with a ``messages`` array.
    The returned dicts are in the source's native format — callers that need
    to iterate content blocks should use format-aware helpers.

    Args:
        path: Path to the transcript file.
        source: Session source (``"claude"``, ``"gemini"``, ``"qwen"``, ``"codex"``, ``"droid"``).
    """
    # Gemini/Qwen JSON session files are a single JSON object, not JSONL.
    if path.suffix == ".json" and source in {"gemini", "qwen"}:
        return await _read_gemini_json_transcript(path)

    # JSONL format (Claude, Codex, default)
    turns: list[dict[str, Any]] = []
    async with aiofiles.open(path, encoding="utf-8") as f:
        async for idx, line in async_enumerate(f):
            if line.strip():
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        turns.append(obj)
                    else:
                        logger.warning(f"Skipping non-dict JSONL value at line {idx + 1} in {path}")
                except json.JSONDecodeError:
                    logger.warning(f"Skipping malformed JSONL line {idx + 1} in {path}")
    return turns


def _summary_source_text(value: str | None) -> str:
    """Normalize optional markdown fields for summary context decisions."""
    return value.strip() if value and value.strip() else ""


def _truncate_markdown(value: str, max_chars: int) -> str:
    """Bound prompt context without splitting through the fallback plumbing."""
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}\n..."


def _format_transcript_fallback_summary(
    turns: list[dict[str, Any]],
    formatter: Any,
) -> str:
    """Format a bounded transcript fallback for sessions without digest markdown."""
    bounded_turns = turns[-TRANSCRIPT_FALLBACK_MAX_TURNS:]
    formatted = formatter(bounded_turns)
    return _truncate_markdown(formatted, TRANSCRIPT_FALLBACK_MAX_CHARS)


def _format_deterministic_summary(handoff_ctx: Any, digest_markdown: str) -> str:
    """Build deterministic markdown when provider generation is unavailable."""
    from gobby.sessions.formatting import format_handoff_as_markdown

    base_markdown = format_handoff_as_markdown(handoff_ctx)
    if not digest_markdown:
        return base_markdown

    digest_section = _truncate_markdown(digest_markdown, DIGEST_FALLBACK_MAX_CHARS)
    return f"## Session Digest\n\n{digest_section}\n\n{base_markdown}".strip()


async def _read_gemini_json_transcript(path: Path) -> list[dict[str, Any]]:
    """Read a Gemini JSON session file and return its native message dicts.

    Gemini session files have the structure::

        {"sessionId": "...", "messages": [{...}, ...], "kind": "main"}

    We return the ``messages`` array as-is so callers get native Gemini dicts.
    """
    async with aiofiles.open(path, encoding="utf-8") as f:
        raw = await f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in Gemini transcript {path}: {e}")
        return []

    if not isinstance(data, dict):
        logger.error(f"Expected JSON object in Gemini transcript {path}, got {type(data).__name__}")
        return []

    messages = data.get("messages", [])
    return [m for m in messages if isinstance(m, dict)]


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
            logger.debug(f"Failed to get git status for {cwd}: {e}")

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
        logger.debug(f"Failed to get git log for {cwd}: {e}")


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
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[str | None, str | None]:
    """Generate the full LLM-based archival summary.

    Returns:
        Tuple of (full_markdown, error_message). One will be None.
    """
    try:
        provider = _resolve_provider(llm_service)

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

        digest_markdown = _summary_source_text(getattr(session, "digest_markdown", None))
        source = getattr(session, "source", None) or "claude"
        first_digest_turn, recent_digest_turns = _extract_digest_turns(digest_markdown)
        if digest_markdown:
            transcript_summary = _truncate_markdown(
                digest_markdown,
                TRANSCRIPT_FALLBACK_MAX_CHARS,
            )
            last_messages_str = recent_digest_turns
        else:
            # Get transcript parser — use the right one for this session's source
            parser: Any
            if source == "gemini":
                from gobby.sessions.transcripts.gemini import GeminiTranscriptParser

                parser = GeminiTranscriptParser()
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

            last_turns = parser.extract_turns_since_clear(turns)
            transcript_summary = _format_transcript_fallback_summary(
                last_turns,
                format_turns_for_llm,
            )
            last_messages = parser.extract_last_messages(turns, num_pairs=2)
            last_messages_str = format_turns_for_llm(last_messages) if last_messages else ""

        file_changes = get_file_changes()
        git_diff_summary = get_git_diff_summary()
        structured_context = _format_structured_context(handoff_ctx)

        # Enrich with DB context
        resolved_db = db or getattr(session_manager, "db", None)
        claimed_tasks = (
            await _run_sqlite(run_db, _get_claimed_tasks, session.id, resolved_db)
            if resolved_db
            else ""
        )
        session_memories = (
            await _run_sqlite(run_db, _get_session_memories, session.id, resolved_db)
            if resolved_db
            else ""
        )
        context = {
            "transcript_summary": transcript_summary,
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
            "session_source": source,
        }

        full_markdown = await provider.generate_summary(context, prompt_template=prompt_template)
        if not is_summary_markdown_valid(full_markdown):
            if full_markdown and full_markdown.strip():
                return None, "Generated session summary was invalid"
            return None, "Generated session summary was empty"
        return full_markdown, None

    except Exception as e:
        logger.error(
            f"Failed to generate full summary for session {session.id}: {e}",
            exc_info=True,
        )
        return None, str(e)


def _get_claimed_tasks(session_id: str, db: DatabaseProtocol) -> str:
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
                logger.debug(f"Failed to get dependencies for task {task.id}: {e}")

            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"Failed to get claimed tasks for session {session_id}: {e}")
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
        logger.debug(f"Failed to get session memories for {session_id}: {e}")
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

    return files_written
