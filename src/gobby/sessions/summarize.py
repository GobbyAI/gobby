"""Transcript-based archival session summary generation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
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
    _source_hash_payload,
    load_summary_prompt_template,
)
from gobby.sessions.summary_generation import (
    _generate_full_summary,
    _persist_summary_markdown,
    _write_files,
)
from gobby.sessions.summary_transcripts import (
    TranscriptWindow,
    _format_transcript_summary,
    _read_first_user_goal,
    _read_transcript_window,
)
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import TranscriptReadError
from gobby.sessions.workspace_context import enrich_git_context as _enrich_git_context
from gobby.sessions.workspace_context import resolve_session_workspace
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SummaryCoreResult:
    result: dict[str, Any]
    full_markdown: str


class SummarySourceContext(NamedTuple):
    window: TranscriptWindow
    turns: list[dict[str, Any]]
    handoff_ctx: HandoffContext
    summary_context: dict[str, Any]
    prompt_template: str
    source_hash: str


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


_SummaryTaskKey = tuple[asyncio.AbstractEventLoop, str]
_summary_tasks: dict[_SummaryTaskKey, asyncio.Task[_SummaryCoreResult]] = {}
_summary_tasks_lock = threading.Lock()


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
    except ImportError as exc:
        logger.debug("Unable to resolve app context for run_db reuse: %s", exc)
        return None
    app_context = app_context_module.get_app_context()
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
    transcript_path = getattr(session, "transcript_path", None)
    path = Path(transcript_path) if transcript_path else None
    if path is None or not path.exists():
        return None
    source = getattr(session, "source", None)
    window = await _read_transcript_window(
        path,
        source=source or "",
        max_records=SUMMARY_ANALYZER_MAX_RECORDS,
    )
    parser_source = _summary_parser_source(source, window.turns)
    parser = get_parser(
        parser_source,
        session_id=getattr(session, "id", None),
        transcript_path=path,
    )
    initial_goal = (
        await _read_first_user_goal(path, source=parser_source) if window.truncated else None
    )
    if parser_source == "claude":
        turns = window.turns
    else:
        turns = analyzer_turns_from_transcript(parser, window.turns)

    handoff_ctx = TranscriptAnalyzer(parser).extract_handoff_context(
        turns, initial_goal=initial_goal
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
    payload = _source_hash_payload(
        session=session,
        summary_context=summary_context,
        prompt_template=prompt_template,
    )
    source_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return SummarySourceContext(
        window=window,
        turns=turns,
        handoff_ctx=handoff_ctx,
        summary_context=summary_context,
        prompt_template=prompt_template,
        source_hash=source_hash,
    )


def _summary_parser_source(source: str | None, turns: list[dict[str, Any]]) -> str:
    if source == "unknown" and any(
        isinstance(turn.get("content"), (str, list))
        and isinstance(turn.get("type"), str)
        and "message" not in turn
        for turn in turns
    ):
        return "qwen"
    if not source:
        raise ValueError("Unsupported transcript source: '<empty>'")
    return source


async def _generate_session_summary_core(
    session_id: str,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None,
    session_summary_config: SessionSummaryConfigProtocol | None,
    db: HubDatabase | None,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> _SummaryCoreResult:
    db_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)
    session = await _run_db(db_runner, session_manager.get, session_id)
    if session is None:
        return _SummaryCoreResult({"success": False, "error": "No session found"}, "")
    try:
        require_local_session_ownership(session)
    except RemoteSessionOwnershipError as exc:
        return _SummaryCoreResult({"success": False, "error": str(exc)}, "")

    try:
        source = await build_summary_source_context(
            session,
            db=db,
            session_manager=session_manager,
            session_summary_config=session_summary_config,
            run_db=db_runner,
        )
    except TranscriptReadError as exc:
        return _SummaryCoreResult({"success": False, "error": str(exc)}, "")
    if source is None:
        return _SummaryCoreResult(
            {"success": False, "error": "Transcript file not found", "session_id": session_id},
            "",
        )

    existing = getattr(session, "summary_markdown", None)
    if getattr(
        session, "summary_source_context_hash", None
    ) == source.source_hash and is_summary_markdown_valid(existing):
        full_markdown = str(existing)
        generation_mode = "noop"
        generation_error = None
    else:
        cwd = resolve_session_workspace(session, getattr(session, "transcript_path", None))
        generated, generation_error = await _generate_full_summary(
            session=session,
            turns=source.turns,
            handoff_ctx=source.handoff_ctx,
            llm_service=llm_service,
            session_summary_config=session_summary_config,
            db=db,
            session_manager=session_manager,
            run_db=db_runner,
            summary_context=source.summary_context,
            prompt_template=source.prompt_template,
            project_path=str(cwd),
        )
        full_markdown = generated or _format_transcript_summary(source.handoff_ctx)
        generation_mode = "full"
        if is_summary_markdown_valid(full_markdown):
            await _persist_summary_markdown(
                session_id=session_id,
                session_manager=session_manager,
                db_runner=db_runner,
                summary_markdown=full_markdown,
                generation_mode=generation_mode,
                source_hash=source.source_hash,
                metadata={"generation_error": generation_error},
            )

    valid = is_summary_markdown_valid(full_markdown)
    wiki_result: dict[str, Any] = {"written": False, "skipped": "invalid_summary"}
    if valid:
        try:
            from gobby.sessions.session_wiki_file import write_session_wiki_page

            wiki_result = await asyncio.to_thread(
                write_session_wiki_page,
                session,
                full_markdown,
            )
        except Exception as exc:
            logger.warning("Session wiki file write failed for session %s: %s", session_id, exc)
            wiki_result = {"written": False, "skipped": "error", "error": str(exc)}

    result: dict[str, Any] = {
        "success": valid,
        "session_id": session_id,
        "full_length": len(full_markdown),
        "generation_mode": generation_mode,
        "generation_error": generation_error,
        "source_context_hash": source.source_hash,
        "session_wiki_file": wiki_result,
        "context_summary": {
            "has_active_task": bool(source.handoff_ctx.active_gobby_task),
            "files_modified_count": len(source.handoff_ctx.files_modified),
            "git_commits_count": len(source.handoff_ctx.git_commits),
            "has_initial_goal": bool(source.handoff_ctx.initial_goal),
        },
    }
    if not valid:
        result["error"] = "Unable to generate a valid session summary"
    return _SummaryCoreResult(result, full_markdown if valid else "")


def _remove_summary_task(
    key: _SummaryTaskKey,
    task: asyncio.Task[_SummaryCoreResult],
) -> None:
    with _summary_tasks_lock:
        if _summary_tasks.get(key) is task:
            del _summary_tasks[key]
    if not task.cancelled():
        task.exception()


async def generate_session_summaries(
    session_id: str,
    session_manager: SessionManagerProtocol,
    llm_service: LLMServiceProtocol | None = None,
    session_summary_config: SessionSummaryConfigProtocol | None = None,
    db: HubDatabase | None = None,
    write_file: bool = False,
    output_path: str = ".gobby/session_summaries",
    set_handoff_ready: bool = False,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Generate or reuse a full transcript-based archival summary."""
    if session_manager is None:
        return {"success": False, "error": "Session manager not available"}
    db_runner = _resolve_run_db(run_db, db=db, session_manager=session_manager)
    loop = asyncio.get_running_loop()
    task_key = (loop, session_id)
    with _summary_tasks_lock:
        task = _summary_tasks.get(task_key)
        if task is None:
            task = loop.create_task(
                _generate_session_summary_core(
                    session_id,
                    session_manager,
                    llm_service,
                    session_summary_config,
                    db,
                    db_runner,
                )
            )
            _summary_tasks[task_key] = task
            task.add_done_callback(partial(_remove_summary_task, task_key))

    core_result = await asyncio.shield(task)
    if set_handoff_ready and core_result.result.get("success"):
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
