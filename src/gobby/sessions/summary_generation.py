"""LLM generation and persistence helpers for session summaries."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.sessions.summarize import (
        LLMServiceProtocol,
        SessionManagerProtocol,
        SessionSummaryConfigProtocol,
    )

logger = logging.getLogger("gobby.sessions.summarize")


def _facade_attr(name: str) -> Any:
    from gobby.sessions import summarize

    return getattr(summarize, name)


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
    run_db = _facade_attr("_run_db")
    if callable(persist_summary_state) and has_concrete_persist:
        await run_db(
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
    """Generate the full LLM-based archival summary.

    Returns:
        Tuple of (full_markdown, error_message). One will be None.
    """
    try:
        if llm_service is None or session_summary_config is None:
            return None, "Session summary LLM feature config not available"

        if prompt_template is None:
            load_summary_prompt_template = _facade_attr("_load_summary_prompt_template")
            prompt_template = load_summary_prompt_template(
                path="handoff/session_end",
                session_summary_config=session_summary_config,
                db=db,
                session_manager=session_manager,
            )

        if not prompt_template:
            return None, "Missing prompt template: handoff/session_end"

        if summary_context is None:
            build_summary_prompt_context = _facade_attr("_build_summary_prompt_context")
            summary_context = await build_summary_prompt_context(
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

        load_summary_prompt_template = _facade_attr("_load_summary_prompt_template")
        prompt_template = load_summary_prompt_template(
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
            cwd=summary_context.get("project_path"),
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
