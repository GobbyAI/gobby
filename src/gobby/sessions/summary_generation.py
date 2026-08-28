"""LLM generation and persistence helpers for archival session summaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles

from gobby.llm.claude_runtime import ClaudeSDKShutdownCancellation
from gobby.sessions.summary_validity import (
    summary_markdown_validation_error,
    summary_prompt_validation_error,
)
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.sessions.summarize import (
        LLMServiceProtocol,
        SessionManagerProtocol,
        SessionSummaryConfigProtocol,
    )

logger = logging.getLogger(__name__)


def _facade_attr(name: str) -> Any:
    from gobby.sessions import summarize

    return getattr(summarize, name)


async def _write_summary_file(
    session_id: str,
    content: str,
    output_path: str | None = None,
    session_manager: Any = None,
    mode: str = "full",
) -> str | None:
    """Write one archival summary file."""
    summary_dir: Path | None = None
    ref = session_id
    try:
        summary_dir = Path(output_path or ".gobby/session_summaries").expanduser()
        summary_dir.mkdir(parents=True, exist_ok=True)
        if session_manager:
            session = session_manager.get(session_id)
            if session:
                seq_num = getattr(session, "seq_num", None)
                if isinstance(seq_num, int) and seq_num > 0:
                    ref = str(seq_num)
                else:
                    candidate = getattr(session, "external_id", None)
                    ref = candidate if isinstance(candidate, str) else session_id
        summary_file = summary_dir / f"{ref}-{mode}.md"
        async with aiofiles.open(summary_file, "w", encoding="utf-8") as stream:
            await stream.write(content)
        return str(summary_file)
    except Exception as exc:
        logger.exception(
            "Failed to write summary file: %s",
            exc,
            extra={"session_id": session_id, "output_dir": str(summary_dir)},
        )
        return None


async def _persist_summary_markdown(
    *,
    session_id: str,
    session_manager: SessionManagerProtocol,
    db_runner: Callable[..., Awaitable[Any]] | None,
    summary_markdown: str,
    generation_mode: str,
    source_hash: str,
    metadata: dict[str, Any],
) -> None:
    persist_summary_state = getattr(session_manager, "persist_summary_state", None)
    has_concrete_persist = callable(getattr(type(session_manager), "persist_summary_state", None))
    run_db = _facade_attr("_run_db")
    if callable(persist_summary_state) and has_concrete_persist:
        await run_db(
            db_runner,
            persist_summary_state,
            session_id,
            summary_markdown=summary_markdown,
            generation_mode=generation_mode,
            source_context_hash=source_hash,
            metadata_json=metadata,
        )
        return
    await run_db(
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
    project_path: str | None = None,
) -> tuple[str | None, str | None]:
    """Generate a full transcript-based archival summary."""
    try:
        if llm_service is None or session_summary_config is None:
            return None, "Session summary LLM feature config not available"
        if prompt_template is None:
            loader = _facade_attr("load_summary_prompt_template")
            prompt_template = loader(
                path="handoff/session_end",
                session_summary_config=session_summary_config,
                db=db,
                session_manager=session_manager,
            )
        if not prompt_template:
            return None, "Missing prompt template: handoff/session_end"
        prompt_error = summary_prompt_validation_error(prompt_template)
        if prompt_error is not None:
            return None, f"Invalid summary prompt template: {prompt_error}"
        if summary_context is None:
            builder = _facade_attr("_build_summary_prompt_context")
            summary_context = await builder(
                session=session,
                turns=turns,
                handoff_ctx=handoff_ctx,
                db=db,
                session_manager=session_manager,
                run_db=run_db,
                project_path=project_path,
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
            cwd=project_path,
            output_validator=summary_markdown_validation_error,
        )
        validation_error = summary_markdown_validation_error(full_markdown)
        if validation_error is not None:
            return None, f"Generated session summary was invalid: {validation_error}"
        return full_markdown, None
    except ClaudeSDKShutdownCancellation as exc:
        raise asyncio.CancelledError(str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "Failed to generate full summary",
            extra={"session_id": session.id, "error": str(exc)},
        )
        return None, str(exc)


async def _write_files(
    session_id: str,
    full_markdown: str | None,
    write_file: bool,
    output_path: str,
    session_manager: SessionManagerProtocol,
) -> list[str]:
    if not write_file or not full_markdown:
        return []
    path = await _write_summary_file(
        session_id,
        full_markdown,
        output_path,
        session_manager,
    )
    return [path] if path else []
