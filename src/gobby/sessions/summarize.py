"""Shared session summary generation.

Single entry point for producing summary_markdown at session boundaries.
Used by:
- MCP set_handoff_context (automated fallback path)
- hook_manager._dispatch_session_summaries (graceful exit via /clear, /exit, /compact)
- SessionLifecycleManager (expired sessions safety net)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from gobby.config.feature_base import FeatureCandidateInput
from gobby.sessions.summary_context import (
    _build_summary_prompt_context,
    _get_claimed_tasks,
    _get_session_memories,
    _load_summary_prompt_template,
    _looks_like_mock,
    _source_hash_payload,
    _summary_context_db,
)
from gobby.sessions.summary_generation import (
    _generate_delta_summary,
    _generate_full_summary,
    _persist_summary_markdown,
    _write_files,
)
from gobby.sessions.summary_refresh import (
    choose_summary_refresh,
    digest_turn_count,
    source_context_hash,
)
from gobby.sessions.summary_transcripts import (
    DIGEST_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_TURNS,
    TURN_PATTERN,
    _digest_markdown_for_summary,
    _extract_digest_turns,
    _format_deterministic_summary,
    _format_transcript_fallback_summary,
    _read_transcript,
    _read_typed_json_transcript,
    _strip_injected_context_from_value,
    _summary_source_text,
    _truncate_markdown,
    async_enumerate,
)
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.sessions.workspace_context import (
    enrich_git_context as _enrich_git_context,
)
from gobby.sessions.workspace_context import (
    resolve_session_workspace,
)
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

__all__ = [
    "DIGEST_FALLBACK_MAX_CHARS",
    "FeatureConfigProtocol",
    "LLMServiceProtocol",
    "SessionManagerProtocol",
    "SessionSummaryConfigProtocol",
    "TRANSCRIPT_FALLBACK_MAX_CHARS",
    "TRANSCRIPT_FALLBACK_MAX_TURNS",
    "TURN_PATTERN",
    "_build_summary_prompt_context",
    "_digest_markdown_for_summary",
    "_enrich_git_context",
    "_extract_digest_turns",
    "_format_deterministic_summary",
    "_format_transcript_fallback_summary",
    "_generate_delta_summary",
    "_generate_full_summary",
    "_get_claimed_tasks",
    "_get_session_memories",
    "_load_summary_prompt_template",
    "_looks_like_mock",
    "_persist_summary_markdown",
    "_read_typed_json_transcript",
    "_read_transcript",
    "_resolve_run_db",
    "_run_db",
    "_source_hash_payload",
    "_strip_injected_context_from_value",
    "_summary_context_db",
    "_summary_source_text",
    "_truncate_markdown",
    "_write_files",
    "async_enumerate",
    "generate_session_summaries",
]


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
    def candidates(self) -> Sequence[FeatureCandidateInput]: ...


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
        cwd: str | None = None,
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
    cwd = resolve_session_workspace(session, transcript_path)
    await _enrich_git_context(handoff_ctx, cwd)

    summary_context = await _build_summary_prompt_context(
        session=session,
        turns=turns,
        handoff_ctx=handoff_ctx,
        db=db,
        session_manager=session_manager,
        run_db=db_runner,
        project_path=str(cwd),
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
            project_path=str(cwd),
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

    logger.info(
        f"Session summary generated for {session_id} ({(len(full_markdown) if full_markdown else 0)} chars)",
    )

    # Tail: the session wiki page IS the summary. Write the redacted
    # summary_markdown to the flat session-wiki file gwiki ingests. Best-effort
    # so a wiki-file failure never breaks summary generation, and hung here so
    # every summary-producing caller (lifecycle/background, dispatcher,
    # CLI/server/MCP refresh) emits the file with no caller drift. Written
    # whenever the summary is valid — including noop refreshes, which restores a
    # missing flat file without re-running the summary LLM.
    final_summary = full_markdown if isinstance(full_markdown, str) else ""
    session_wiki_result: dict[str, Any] = {"written": False, "skipped": "invalid_summary"}
    try:
        from gobby.sessions.session_wiki_file import write_session_wiki_page

        session_wiki_result = await asyncio.to_thread(
            write_session_wiki_page,
            session,
            final_summary,
        )
    except Exception as e:  # noqa: BLE001 — boundary: wiki file must not break summary
        logger.warning(f"Session wiki file write failed for session {session_id}: {e}")
        session_wiki_result = {"written": False, "skipped": "error", "error": str(e)}

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
        "session_wiki_file": session_wiki_result,
        "context_summary": {
            "has_active_task": bool(handoff_ctx.active_gobby_task),
            "files_modified_count": len(handoff_ctx.files_modified),
            "git_commits_count": len(handoff_ctx.git_commits),
            "has_initial_goal": bool(handoff_ctx.initial_goal),
        },
    }
