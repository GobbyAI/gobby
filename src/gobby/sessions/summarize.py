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
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import aiofiles

from gobby.sessions.summary_refresh import (
    choose_summary_refresh,
    digest_turn_count,
    source_context_hash,
)
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.storage.hub.protocol import HubDatabase

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
    def persist_summary_state(
        self,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = ...,
        source_digest_turn_count: int | None = ...,
        metadata_json: dict[str, Any] | None = ...,
        summary_path: str | None = ...,
    ) -> Any: ...
    def update_status(self, session_id: str, status: str) -> Any: ...


class FeatureConfigProtocol(Protocol):
    @property
    def profile(self) -> Any: ...

    @property
    def candidates(self) -> Sequence[str]: ...


class SessionSummaryConfigProtocol(FeatureConfigProtocol, Protocol):
    @property
    def prompt(self) -> str | None: ...


class LLMServiceProtocol(Protocol):
    async def call_feature(
        self,
        feature_config: FeatureConfigProtocol,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
    ) -> str: ...


async def _run_db(
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
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
) -> Callable[..., Awaitable[Any]] | None:
    if explicit_run_db is not None:
        return explicit_run_db

    resolved_db = db or getattr(session_manager, "db", None)
    if resolved_db is None:
        return None

    try:
        from gobby import app_context as app_context_module

        get_app_context = app_context_module.get_app_context
    except (ImportError, AttributeError) as exc:
        logger.debug("Unable to resolve app context for run_db reuse: %s", exc)
        return None

    app_context = get_app_context()

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
    session_summary_config: SessionSummaryConfigProtocol | None = None,
    db: HubDatabase | None = None,
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
        session_summary_config: Feature config for summary generation.
        db: Database for prompt template loading.
        write_file: Write summary files to disk.
        output_path: Directory for summary files.
        set_handoff_ready: Update session status to handoff_ready.
        compact_only: Ignored (kept for API compatibility).
        full_only: Ignored (kept for API compatibility).
        run_db: Optional bounded executor bridge for hub database storage calls.

    Returns:
        Dict with success status, markdown lengths, and context summary.
    """
    if not session_manager:
        return {"success": False, "error": "Session manager not available"}

    db_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)

    session = await _run_db(db_runner, session_manager.get, session_id)
    if not session:
        return {"success": False, "error": "No session found", "session_id": session_id}

    digest_markdown = _digest_markdown_for_summary(session)
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

    summary_context = await _build_summary_prompt_context(
        session=session,
        turns=turns,
        handoff_ctx=handoff_ctx,
        db=db,
        session_manager=session_manager,
        run_db=db_runner,
    )
    full_prompt_template = _load_summary_prompt_template(
        path="handoff/session_end",
        session_summary_config=session_summary_config,
        db=db,
        session_manager=session_manager,
        allow_runtime_db=False,
    )
    source_hash = source_context_hash(
        _source_hash_payload(
            session=session,
            digest_markdown=digest_markdown,
            summary_context=summary_context,
            prompt_template=full_prompt_template,
        )
    )
    current_digest_turn_count = digest_turn_count(digest_markdown)
    decision = choose_summary_refresh(
        current_source_hash=source_hash,
        current_digest_turn_count=current_digest_turn_count,
        previous_source_hash=getattr(session, "summary_source_context_hash", None),
        previous_digest_turn_count=getattr(session, "summary_digest_turn_count", None),
        previous_summary_valid=is_summary_markdown_valid(
            getattr(session, "summary_markdown", None)
        ),
        digest_markdown=digest_markdown,
    )

    full_error: str | None = None
    delta_error: str | None = None
    generation_mode = decision.mode
    full_markdown = getattr(session, "summary_markdown", None) if decision.mode == "noop" else None

    if decision.mode == "delta":
        full_markdown, delta_error = await _generate_delta_summary(
            session=session,
            previous_summary=getattr(session, "summary_markdown", "") or "",
            new_digest_turns=decision.new_digest_turns,
            summary_context=summary_context,
            llm_service=llm_service,
            session_summary_config=session_summary_config,
            db=db,
            session_manager=session_manager,
        )
        if not is_summary_markdown_valid(full_markdown):
            logger.warning(
                "Delta summary merge failed for %s (%s), falling back to full generation",
                session_id,
                delta_error,
            )
            generation_mode = "full"

    if decision.mode == "full" or (
        generation_mode == "full" and not is_summary_markdown_valid(full_markdown)
    ):
        full_markdown, full_error = await _generate_full_summary(
            session=session,
            turns=turns,
            handoff_ctx=handoff_ctx,
            llm_service=llm_service,
            session_summary_config=session_summary_config,
            db=db,
            session_manager=session_manager,
            run_db=db_runner,
            summary_context=summary_context,
            prompt_template=full_prompt_template,
        )

    if decision.mode != "noop" and not is_summary_markdown_valid(full_markdown):
        logger.warning(
            f"Full LLM summary failed ({full_error}), falling back to code-only",
        )
        full_markdown = _format_deterministic_summary(handoff_ctx, digest_markdown)
        generation_mode = "digest_fallback"

    # Persist to database
    if decision.mode != "noop" and is_summary_markdown_valid(full_markdown):
        summary_text = full_markdown if isinstance(full_markdown, str) else ""
        metadata = {
            "reason": decision.reason,
            "delta_error": delta_error,
            "full_error": full_error,
        }
        await _persist_summary_markdown(
            session_id=session_id,
            session_manager=session_manager,
            db_runner=db_runner,
            summary_markdown=summary_text,
            generation_mode=generation_mode,
            source_hash=source_hash,
            digest_turns=current_digest_turn_count,
            metadata=metadata,
        )

    # Set handoff_ready status
    if set_handoff_ready:
        await _run_db(db_runner, session_manager.update_status, session_id, "handoff_ready")

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

            await _run_db(
                db_runner,
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
        "delta_error": delta_error,
        "generation_mode": generation_mode,
        "refresh_reason": decision.reason,
        "source_context_hash": source_hash,
        "source_digest_turn_count": current_digest_turn_count,
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
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _digest_markdown_for_summary(session: Any) -> str:
    """Return digest context with the latest completed turn when digest lags."""
    digest_markdown = _summary_source_text(getattr(session, "digest_markdown", None))
    pending_turns = [
        _summary_source_text(getattr(session, "last_turn_markdown", None)),
        _summary_source_text(getattr(session, "last_assistant_content", None)),
    ]

    summary_parts = [digest_markdown] if digest_markdown else []
    next_turn = len(TURN_PATTERN.findall(digest_markdown)) + 1
    for turn_markdown in pending_turns:
        if not turn_markdown:
            continue
        joined_summary = "\n\n".join(summary_parts)
        if turn_markdown in joined_summary:
            continue
        summary_parts.append(f"### Turn {next_turn}\n{turn_markdown}")
        next_turn += 1

    return "\n\n".join(summary_parts)


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


def _load_summary_prompt_template(
    *,
    path: str,
    session_summary_config: SessionSummaryConfigProtocol | None,
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
    allow_runtime_db: bool = True,
) -> str | None:
    prompt_template = getattr(session_summary_config, "prompt", None)
    resolved_db = _summary_context_db(db, session_manager)
    if resolved_db is None and not allow_runtime_db:
        return prompt_template

    try:
        from gobby.prompts.loader import PromptLoader

        loader = PromptLoader(db=resolved_db)
        prompt_obj = loader.load(path)
        prompt_template = prompt_obj.content
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("Failed to load summary prompt %s: %s", path, e)

    return prompt_template


async def _build_summary_prompt_context(
    *,
    session: Any,
    turns: list[dict[str, Any]],
    handoff_ctx: Any,
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    from gobby.workflows.git_utils import get_file_changes, get_git_diff_summary
    from gobby.workflows.summary_actions import (
        _format_structured_context,
        format_turns_for_llm,
    )

    digest_markdown = _digest_markdown_for_summary(session)
    source = getattr(session, "source", None) or "claude"
    first_digest_turn, recent_digest_turns = _extract_digest_turns(digest_markdown)
    if digest_markdown:
        transcript_summary = _truncate_markdown(
            digest_markdown,
            TRANSCRIPT_FALLBACK_MAX_CHARS,
        )
        last_messages_str = recent_digest_turns
    else:
        parser: Any
        if source == "gemini":
            from gobby.sessions.transcripts.gemini import GeminiTranscriptParser

            parser = GeminiTranscriptParser()
        elif source == "qwen":
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

        last_turns = parser.extract_turns_since_clear(turns)
        transcript_summary = _format_transcript_fallback_summary(
            last_turns,
            format_turns_for_llm,
        )
        last_messages = parser.extract_last_messages(turns, num_pairs=2)
        last_messages_str = format_turns_for_llm(last_messages) if last_messages else ""

    resolved_db = _summary_context_db(db, session_manager)
    claimed_tasks = (
        await _run_db(run_db, _get_claimed_tasks, session.id, resolved_db) if resolved_db else ""
    )
    session_memories = (
        await _run_db(run_db, _get_session_memories, session.id, resolved_db) if resolved_db else ""
    )

    return {
        "transcript_summary": transcript_summary,
        "last_messages": last_messages_str,
        "git_status": handoff_ctx.git_status or "",
        "file_changes": get_file_changes(),
        "git_diff_summary": get_git_diff_summary(),
        "structured_context": _format_structured_context(handoff_ctx),
        "claimed_tasks": claimed_tasks,
        "session_memories": session_memories,
        "first_digest_turn": first_digest_turn,
        "recent_digest_turns": recent_digest_turns,
        "external_id": session.id[:12],
        "session_id": session.id,
        "session_source": source,
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


async def _persist_summary_markdown(
    *,
    session_id: str,
    session_manager: SessionManagerProtocol,
    db_runner: Callable[..., Awaitable[Any]] | None,
    summary_markdown: str,
    generation_mode: str,
    source_hash: str,
    digest_turns: int,
    metadata: dict[str, Any],
) -> None:
    persist_summary_state = getattr(session_manager, "persist_summary_state", None)
    has_concrete_persist = callable(getattr(type(session_manager), "persist_summary_state", None))
    if callable(persist_summary_state) and has_concrete_persist:
        await _run_db(
            db_runner,
            persist_summary_state,
            session_id,
            summary_markdown=summary_markdown,
            generation_mode=generation_mode,
            source_context_hash=source_hash,
            source_digest_turn_count=digest_turns,
            metadata_json=metadata,
        )
        return

    await _run_db(
        db_runner,
        session_manager.update_summary,
        session_id,
        summary_markdown=summary_markdown,
    )


async def _generate_full_summary(
    session: Any,
    turns: list[dict[str, Any]],
    handoff_ctx: Any,
    llm_service: LLMServiceProtocol | None,
    session_summary_config: SessionSummaryConfigProtocol | None,
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    summary_context: dict[str, Any] | None = None,
    prompt_template: str | None = None,
) -> tuple[str | None, str | None]:
    """Generate the full LLM-based archival summary.

    Returns:
        Tuple of (full_markdown, error_message). One will be None.
    """
    try:
        if llm_service is None or session_summary_config is None:
            return None, "Session summary LLM feature config not available"

        if prompt_template is None:
            prompt_template = _load_summary_prompt_template(
                path="handoff/session_end",
                session_summary_config=session_summary_config,
                db=db,
                session_manager=session_manager,
            )

        if not prompt_template:
            return None, "Missing prompt template: handoff/session_end"

        if summary_context is None:
            summary_context = await _build_summary_prompt_context(
                session=session,
                turns=turns,
                handoff_ctx=handoff_ctx,
                db=db,
                session_manager=session_manager,
                run_db=run_db,
            )

        from gobby.llm.prompt_rendering import render_summary_prompt

        prompt = render_summary_prompt(prompt_template, summary_context)
        full_markdown = await llm_service.call_feature(
            session_summary_config,
            prompt,
            system_prompt=(
                "You are a session summary generator. Create comprehensive, actionable summaries."
            ),
            caller="sessions.summary",
        )
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


async def _generate_delta_summary(
    *,
    session: Any,
    previous_summary: str,
    new_digest_turns: str,
    summary_context: dict[str, Any],
    llm_service: LLMServiceProtocol | None,
    session_summary_config: SessionSummaryConfigProtocol | None,
    db: HubDatabase | None,
    session_manager: SessionManagerProtocol,
) -> tuple[str | None, str | None]:
    """Merge new digest/context into an existing complete summary."""
    try:
        if llm_service is None or session_summary_config is None:
            return None, "Session summary LLM feature config not available"

        prompt_template = _load_summary_prompt_template(
            path="handoff/session_delta_merge",
            session_summary_config=None,
            db=db,
            session_manager=session_manager,
        )
        if not prompt_template:
            return None, "Missing prompt template: handoff/session_delta_merge"

        context = dict(summary_context)
        context.update(
            {
                "previous_summary": previous_summary,
                "new_digest_turns": new_digest_turns,
            }
        )

        from gobby.llm.prompt_rendering import render_summary_prompt

        prompt = render_summary_prompt(prompt_template, context)
        merged_markdown = await llm_service.call_feature(
            session_summary_config,
            prompt,
            system_prompt=(
                "You are a session summary merger. Return one complete replacement handoff."
            ),
            caller="sessions.summary.delta",
        )
        if not is_summary_markdown_valid(merged_markdown):
            if merged_markdown and merged_markdown.strip():
                return None, "Generated delta session summary was invalid"
            return None, "Generated delta session summary was empty"
        return merged_markdown, None
    except Exception as e:
        logger.error(
            f"Failed to merge summary delta for session {session.id}: {e}",
            exc_info=True,
        )
        return None, str(e)


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
                logger.debug(f"Failed to get dependencies for task {task.id}: {e}")

            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"Failed to get claimed tasks for session {session_id}: {e}")
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
