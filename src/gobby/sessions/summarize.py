"""Shared session summary generation.

Single entry point for producing summary_markdown at session boundaries.
Used by:
- MCP set_handoff_context (automated fallback path)
- hook_manager._dispatch_session_summaries (graceful exit via /clear, /exit, /compact)
- SessionLifecycleManager (expired sessions safety net)
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from gobby.config.feature_base import FeatureCandidateInput
from gobby.sessions.analyzer import HandoffContext, TranscriptAnalyzer
from gobby.sessions.analyzer_turns import (
    SUMMARY_ANALYZER_MAX_RECORDS,
    analyzer_turns_from_transcript,
)
from gobby.sessions.machine_scope import (
    RemoteSessionOwnershipError,
    require_local_session_ownership,
)
from gobby.sessions.summary_context import (
    _build_summary_prompt_context,
    _get_claimed_tasks,
    _get_session_memories,
    _looks_like_mock,
    _source_hash_payload,
    _summary_context_db,
    load_summary_prompt_template,
)
from gobby.sessions.summary_generation import (
    _generate_delta_summary,
    _generate_full_summary,
    _persist_summary_markdown,
    _write_files,
)
from gobby.sessions.summary_refresh import (
    choose_summary_refresh,
    coerce_digest_turn_count,
    digest_turn_count,
    source_context_hash,
)
from gobby.sessions.summary_transcripts import (
    DIGEST_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_TURNS,
    TURN_PATTERN,
    TranscriptWindow,
    _digest_markdown_for_summary,
    _extract_digest_turns,
    _format_deterministic_summary,
    _format_transcript_fallback_summary,
    _read_first_user_goal,
    _read_transcript,
    _read_transcript_window,
    _strip_injected_context_from_value,
    _summary_source_text,
    _truncate_markdown,
    async_enumerate,
)
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import TranscriptReadError
from gobby.sessions.workspace_context import (
    enrich_git_context as _enrich_git_context,
)
from gobby.sessions.workspace_context import (
    resolve_session_workspace,
)
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SummaryCoreResult:
    result: dict[str, Any]
    full_markdown: str


class SummarySourceContext(NamedTuple):
    digest_markdown: str
    window: TranscriptWindow
    turns: list[dict[str, Any]]
    handoff_ctx: HandoffContext
    summary_context: dict[str, Any]
    prompt_template: str
    source_hash: str


_SummaryTaskKey = tuple[asyncio.AbstractEventLoop, str]
_summary_tasks: dict[_SummaryTaskKey, asyncio.Task[_SummaryCoreResult]] = {}
_summary_tasks_lock = threading.Lock()


def _remove_summary_task(
    key: _SummaryTaskKey,
    task: asyncio.Task[_SummaryCoreResult],
) -> None:
    with _summary_tasks_lock:
        if _summary_tasks.get(key) is task:
            del _summary_tasks[key]
    if not task.cancelled():
        task.exception()


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
    "_looks_like_mock",
    "_persist_summary_markdown",
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
    "load_summary_prompt_template",
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
        output_validator: Callable[[str], str | None] | None = None,
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


async def build_summary_source_context(
    session: Any,
    *,
    db: Any,
    session_manager: Any,
    session_summary_config: Any,
    run_db: Any = None,
) -> SummarySourceContext | None:
    """Build the canonical transcript-derived source payload for a summary."""
    digest_markdown = _digest_markdown_for_summary(session)
    transcript_path = getattr(session, "transcript_path", None)
    path = Path(transcript_path) if transcript_path else None
    source = getattr(session, "source", None) or "claude"
    window = TranscriptWindow(turns=[], truncated=False)

    if path is not None and path.exists():
        window = await _read_transcript_window(
            path,
            source=source,
            max_records=SUMMARY_ANALYZER_MAX_RECORDS,
        )
    elif not digest_markdown:
        return None

    parser_source = _summary_parser_source(source, window.turns)
    initial_goal = (
        await _read_first_user_goal(path, source=parser_source)
        if path is not None and window.truncated
        else None
    )
    if parser_source == "claude":
        turns = window.turns
    else:
        parser = get_parser(
            parser_source,
            session_id=getattr(session, "id", None),
            transcript_path=path,
        )
        turns = analyzer_turns_from_transcript(parser, window.turns)

    handoff_ctx = TranscriptAnalyzer().extract_handoff_context(
        turns,
        initial_goal=initial_goal,
    )
    cwd = resolve_session_workspace(session, transcript_path)
    await _enrich_git_context(handoff_ctx, cwd)
    db_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)
    summary_context = await _build_summary_prompt_context(
        session=session,
        turns=window.turns,
        handoff_ctx=handoff_ctx,
        db=db,
        session_manager=session_manager,
        run_db=db_runner,
        project_path=str(cwd),
    )
    prompt_template = (
        load_summary_prompt_template(
            path="handoff/session_end",
            session_summary_config=session_summary_config,
            db=db,
            session_manager=session_manager,
        )
        or ""
    )
    source_hash = source_context_hash(
        _source_hash_payload(
            session=session,
            digest_markdown=digest_markdown,
            summary_context=summary_context,
            prompt_template=prompt_template,
        )
    )
    return SummarySourceContext(
        digest_markdown=digest_markdown,
        window=window,
        turns=turns,
        handoff_ctx=handoff_ctx,
        summary_context=summary_context,
        prompt_template=prompt_template,
        source_hash=source_hash,
    )


def _summary_parser_source(source: str, turns: list[dict[str, Any]]) -> str:
    if source != "unknown":
        return source
    if any(
        isinstance(turn.get("content"), (str, list))
        and isinstance(turn.get("type"), str)
        and "message" not in turn
        for turn in turns
    ):
        return "qwen"
    return "claude"


async def _generate_session_summary_core(
    session_id: str,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None = None,
    session_summary_config: SessionSummaryConfigProtocol | None = None,
    db: HubDatabase | None = None,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> _SummaryCoreResult:
    db_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)

    session = await _run_db(db_runner, session_manager.get, session_id)
    if not session:
        return _SummaryCoreResult(
            result={"success": False, "error": "No session found", "session_id": session_id},
            full_markdown="",
        )

    try:
        require_local_session_ownership(session)
    except RemoteSessionOwnershipError as exc:
        return _SummaryCoreResult(
            result={"success": False, "error": str(exc), "session_id": session_id},
            full_markdown="",
        )

    transcript_path = getattr(session, "transcript_path", None)
    try:
        source_context = await build_summary_source_context(
            session,
            db=db,
            session_manager=session_manager,
            session_summary_config=session_summary_config,
            run_db=db_runner,
        )
    except TranscriptReadError as exc:
        return _SummaryCoreResult(
            result={
                "success": False,
                "error": str(exc),
                "session_id": session_id,
            },
            full_markdown="",
        )

    if source_context is None:
        if not transcript_path:
            return _SummaryCoreResult(
                result={
                    "success": False,
                    "error": "No transcript path for session",
                    "session_id": session_id,
                },
                full_markdown="",
            )
        return _SummaryCoreResult(
            result={
                "success": False,
                "error": "Transcript file not found",
                "path": transcript_path,
            },
            full_markdown="",
        )

    digest_markdown = source_context.digest_markdown
    turns = source_context.turns
    handoff_ctx = source_context.handoff_ctx
    summary_context = source_context.summary_context
    full_prompt_template = source_context.prompt_template
    source_hash = source_context.source_hash
    cwd = resolve_session_workspace(session, transcript_path)
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
            "Full LLM summary failed for %s (%s), falling back to code-only",
            session_id,
            full_error,
        )
        full_markdown = _format_deterministic_summary(handoff_ctx, digest_markdown)
        generation_mode = "digest_fallback"

    summary_is_valid = is_summary_markdown_valid(full_markdown)

    # Persist to database
    if decision.mode != "noop" and summary_is_valid:
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

    summary_length = len(full_markdown) if full_markdown else 0
    if decision.mode == "noop":
        logger.debug(
            "Session summary unchanged for %s (mode=noop, reason=%s, output_chars=%s)",
            session_id,
            decision.reason,
            summary_length,
        )
    else:
        logger.debug(
            "Session summary generated for %s (mode=%s, reason=%s, output_chars=%s)",
            session_id,
            generation_mode,
            decision.reason,
            summary_length,
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
    except Exception as e:  # Wiki persistence must not break an otherwise valid summary.
        logger.warning("Session wiki file write failed for session %s: %s", session_id, e)
        session_wiki_result = {"written": False, "skipped": "error", "error": str(e)}

    result = {
        "success": summary_is_valid,
        "session_id": session_id,
        "compact_length": 0,  # Kept for API compatibility
        "full_length": len(full_markdown) if full_markdown else 0,
        "full_error": full_error,
        "delta_error": delta_error,
        "generation_mode": generation_mode,
        "refresh_reason": decision.reason,
        "source_context_hash": source_hash,
        "source_digest_turn_count": current_digest_turn_count,
        "session_wiki_file": session_wiki_result,
        "context_summary": {
            "has_active_task": bool(handoff_ctx.active_gobby_task),
            "files_modified_count": len(handoff_ctx.files_modified),
            "git_commits_count": len(handoff_ctx.git_commits),
            "has_initial_goal": bool(handoff_ctx.initial_goal),
        },
    }
    if not summary_is_valid:
        result["error"] = "Unable to generate a valid session summary"
    return _SummaryCoreResult(result=result, full_markdown=final_summary)


async def generate_session_summaries(
    session_id: str,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None = None,
    session_summary_config: SessionSummaryConfigProtocol | None = None,
    db: HubDatabase | None = None,
    write_file: bool = False,
    output_path: str = ".gobby/session_summaries",
    set_handoff_ready: bool = False,
    compact_only: bool = False,
    full_only: bool = False,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Generate summary_markdown for a session.

    Concurrent requests for one session on the same event loop share load,
    generation, persistence, and wiki output. Joiners reuse the originator's
    LLM service, summary configuration, database, session manager, and database
    runner. Different event loops generate independently. Status transitions
    and optional file output remain caller-specific.

    Args:
        session_id: Platform session ID (UUID).
        session_manager: SessionManager instance.
        llm_service: LLM service for generating summaries.
        session_summary_config: Feature config for summary generation.
        db: Database for prompt template loading.
        write_file: Write summary files to disk.
        output_path: Directory for summary files.
        set_handoff_ready: Update session status to handoff_ready. Only
            synchronous, deliberate handoff paths may pass True; delayed or
            background refreshes must leave lifecycle status to the
            synchronous lifecycle handlers.
        compact_only: Ignored (kept for API compatibility).
        full_only: Ignored (kept for API compatibility).
        run_db: Optional bounded executor bridge for hub database storage calls.

    Returns:
        Dict with success status, markdown lengths, and context summary.
    """
    if not session_manager:
        return {"success": False, "error": "Session manager not available"}

    db_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)
    loop = asyncio.get_running_loop()
    task_key = (loop, session_id)
    with _summary_tasks_lock:
        task = _summary_tasks.get(task_key)
        if task is None:
            task = loop.create_task(
                _generate_session_summary_core(
                    session_id=session_id,
                    session_manager=session_manager,
                    llm_service=llm_service,
                    session_summary_config=session_summary_config,
                    db=db,
                    run_db=db_runner,
                )
            )
            _summary_tasks[task_key] = task
            task.add_done_callback(partial(_remove_summary_task, task_key))
        else:
            logger.debug("Joining in-flight session summary generation for %s", session_id)

    core_result = await asyncio.shield(task)
    summary_is_valid = bool(core_result.result.get("success"))

    if set_handoff_ready and summary_is_valid:
        await _run_db(db_runner, session_manager.update_status, session_id, "handoff_ready")

    files_written = await _write_files(
        session_id=session_id,
        full_markdown=core_result.full_markdown,
        write_file=write_file,
        output_path=output_path,
        session_manager=session_manager,
    )
    result = copy.deepcopy(core_result.result)
    result["files_written"] = files_written
    return result


async def refresh_session_summary_to_watermark(
    *,
    session_id: str,
    minimum_digest_turn_count: int,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None = None,
    session_summary_config: SessionSummaryConfigProtocol | None = None,
    db: HubDatabase | None = None,
) -> dict[str, Any]:
    """Run a scheduled refresh and catch up after joining stale in-flight work."""
    result = await generate_session_summaries(
        session_id=session_id,
        session_manager=session_manager,
        llm_service=llm_service,
        session_summary_config=session_summary_config,
        db=db,
        set_handoff_ready=False,
    )
    observed = coerce_digest_turn_count(result.get("source_digest_turn_count"))
    if result.get("success") and observed is not None and observed >= minimum_digest_turn_count:
        return result

    if not result.get("success"):
        reason = result.get("error") or result.get("refresh_reason") or "unknown"
        logger.warning(
            "Scheduled session summary refresh skipped for %s (target_digest_turns=%s, reason=%s)",
            session_id,
            minimum_digest_turn_count,
            reason,
        )
        return result

    logger.debug(
        "Scheduled session summary refresh for %s joined stale generation "
        "(target_digest_turns=%s, observed_digest_turns=%s); retrying",
        session_id,
        minimum_digest_turn_count,
        observed,
    )
    result = await generate_session_summaries(
        session_id=session_id,
        session_manager=session_manager,
        llm_service=llm_service,
        session_summary_config=session_summary_config,
        db=db,
        set_handoff_ready=False,
    )
    observed = coerce_digest_turn_count(result.get("source_digest_turn_count"))
    if result.get("success") and observed is not None and observed >= minimum_digest_turn_count:
        return result

    reason = result.get("error") or result.get("refresh_reason") or "no_watermark_progress"
    logger.warning(
        "Scheduled session summary refresh did not reach its watermark for %s "
        "(target_digest_turns=%s, observed_digest_turns=%s, reason=%s)",
        session_id,
        minimum_digest_turn_count,
        observed,
        reason,
    )
    return result
