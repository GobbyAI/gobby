"""LLM generation and persistence helpers for session summaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import aiofiles

from gobby.hooks.tool_error_tracker import load_open_tool_errors
from gobby.llm.claude_runtime import ClaudeSDKShutdownCancellation
from gobby.sessions.machine_scope import (
    RemoteSessionOwnershipError,
    require_local_session_ownership,
)
from gobby.sessions.summary_context import load_summary_prompt_template
from gobby.sessions.summary_formatting import (
    _format_structured_context,
    format_turns_for_llm,
    format_unresolved_errors,
)
from gobby.sessions.summary_transcripts import (
    DIGEST_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_TURNS,
    _format_transcript_fallback_summary,
    _read_transcript,
    _truncate_markdown,
)
from gobby.sessions.summary_validity import (
    summary_markdown_validation_error,
    summary_prompt_validation_error,
)
from gobby.sessions.workspace_context import resolve_session_workspace
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.git_utils import (
    get_file_changes,
    get_git_diff_summary,
    get_git_status,
    get_recent_git_commits,
)

if TYPE_CHECKING:
    from gobby.config.sessions import SessionSummaryConfig
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
    mode: str = "clear",
) -> str | None:
    """Write summary to file in session_summaries directory.

    Files are named ``{seq_num}-{mode}.md`` (e.g. ``2139-clear.md``).
    Falls back to ``{external_id}-{mode}.md`` or ``{session_id}-{mode}.md``
    when seq_num is unavailable.

    Args:
        session_id: Internal session ID
        content: Summary markdown content
        output_path: Override directory for summary files
        session_manager: Session manager to look up seq_num / external_id
        mode: "clear" or "compact" — used as filename suffix

    Returns:
        Path to written file, or None on failure
    """
    summary_dir: Path | None = None
    ref: str = session_id
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
                    ref = getattr(session, "external_id", None) or session_id
                    if not isinstance(ref, str):
                        ref = session_id

        summary_file = summary_dir / f"{ref}-{mode}.md"

        async with aiofiles.open(summary_file, "w", encoding="utf-8") as f:
            await f.write(content)

        logger.debug(
            "Session summary written",
            extra={
                "session_id": session_id,
                "ref": ref,
                "summary_file": str(summary_file),
                "output_dir": str(summary_dir),
            },
        )
        return str(summary_file)
    except Exception as e:
        logger.exception(
            "Failed to write summary file: %s",
            e,
            extra={
                "session_id": session_id,
                "ref": ref,
                "output_dir": str(summary_dir) if summary_dir is not None else None,
            },
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
            load_summary_prompt_template = _facade_attr("load_summary_prompt_template")
            prompt_template = load_summary_prompt_template(
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
            output_validator=summary_markdown_validation_error,
        )
        validation_error = summary_markdown_validation_error(full_markdown)
        if validation_error is not None:
            return None, f"Generated session summary was invalid: {validation_error}"
        return full_markdown, None

    except ClaudeSDKShutdownCancellation as e:
        logger.info(
            "Session summary generation cancelled during daemon shutdown",
            extra={"session_id": session.id},
        )
        raise asyncio.CancelledError(str(e)) from e
    except Exception as e:
        logger.exception(
            "Failed to generate full summary",
            extra={"session_id": session.id, "error": str(e)},
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

        load_summary_prompt_template = _facade_attr("load_summary_prompt_template")
        prompt_template = load_summary_prompt_template(
            path="handoff/session_delta_merge",
            session_summary_config=None,
            db=db,
            session_manager=session_manager,
        )
        if not prompt_template:
            return None, "Missing prompt template: handoff/session_delta_merge"

        prompt_error = summary_prompt_validation_error(prompt_template)
        if prompt_error is not None:
            return None, f"Invalid delta summary prompt template: {prompt_error}"

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
            output_validator=summary_markdown_validation_error,
        )
        validation_error = summary_markdown_validation_error(merged_markdown)
        if validation_error is not None:
            return None, f"Generated delta session summary was invalid: {validation_error}"
        return merged_markdown, None
    except Exception as e:
        logger.exception(
            "Failed to merge summary delta",
            extra={"session_id": session.id, "error": str(e)},
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

    if full_markdown:
        full_path: str | None = await _write_summary_file(
            session_id, full_markdown, output_path, session_manager, mode="full"
        )
        if full_path:
            files_written.append(full_path)

    return files_written


async def generate_summary(
    session_manager: Any,
    session_id: str,
    llm_service: Any,
    transcript_processor: Any,
    session_summary_config: SessionSummaryConfig | None = None,
    template: str | None = None,
    previous_summary: str | None = None,
    mode: Literal["clear", "compact"] = "clear",
    write_file: bool = False,
    output_path: str | None = None,
) -> dict[str, Any] | None:
    """Generate a session summary using LLM and store it in the session record.

    Args:
        session_manager: The session manager instance
        session_id: Current session ID
        llm_service: LLM service instance
        transcript_processor: Transcript processor instance
        session_summary_config: Feature config for summary generation
        template: Optional prompt template
        previous_summary: Previous summary_markdown for cumulative compression (compact mode)
        mode: "clear" or "compact" - passed to LLM context to control summarization density

    Returns:
        Dict with summary_generated and summary_length, or error

    Raises:
        ValueError: If mode is not "clear" or "compact"
    """
    # Validate mode parameter
    valid_modes = {"clear", "compact"}
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}")

    if not llm_service or not transcript_processor or session_summary_config is None:
        logger.warning("generate_summary: Missing LLM service, transcript processor, or config")
        return {"error": "Missing services"}

    current_session = await asyncio.to_thread(session_manager.get, session_id)
    if not current_session:
        return {"error": "Session not found"}

    try:
        require_local_session_ownership(current_session)
    except RemoteSessionOwnershipError as exc:
        return {"error": str(exc)}

    transcript_path = getattr(current_session, "transcript_path", None)
    if not transcript_path:
        logger.warning("generate_summary: No transcript path for session %s", session_id)
        return {"error": "No transcript path"}

    if not template:
        template = await asyncio.to_thread(
            load_summary_prompt_template,
            path="handoff/session_end",
            session_summary_config=session_summary_config,
            db=getattr(session_manager, "db", None),
            session_manager=session_manager,
        )

    prompt_error = summary_prompt_validation_error(template)
    if prompt_error is not None:
        logger.warning(
            "Invalid on-demand summary prompt",
            extra={"session_id": session_id, "error": prompt_error},
        )
        return {"error": f"Invalid summary prompt template: {prompt_error}"}
    template = cast(str, template)

    # 1. Process Transcript
    try:
        transcript_file = Path(transcript_path)
        if not transcript_file.exists():
            logger.warning("Transcript file not found: %s", transcript_path)
            return {"error": "Transcript not found"}

        source = getattr(current_session, "source", None) or "claude"
        turns = await _read_transcript(
            transcript_file,
            source=source,
            max_turns=150,
        )

        # Turn extraction is deliberately mode-agnostic: we always extract the most
        # recent turns since the last /clear and let the prompt control summarization
        # density. The mode parameter is passed to the LLM context where the template
        # can adjust output format (e.g., compact mode may instruct denser summaries).
        recent_turns = transcript_processor.extract_turns_since_clear(turns, max_turns=100)

        # Format turns for LLM
        transcript_summary = _format_transcript_fallback_summary(
            recent_turns,
            format_turns_for_llm,
        )
    except Exception as e:
        logger.error("Failed to process transcript: %s", e)
        return {"error": str(e)}

    # 2. Gather context variables for template
    last_messages = transcript_processor.extract_last_messages(recent_turns, num_pairs=2)
    last_messages_str = (
        _truncate_markdown(format_turns_for_llm(last_messages), TRANSCRIPT_FALLBACK_MAX_CHARS)
        if last_messages
        else ""
    )

    # Get git status and file changes
    project_path = str(resolve_session_workspace(current_session, transcript_path))
    git_status, file_changes, git_diff_summary = await asyncio.gather(
        asyncio.to_thread(get_git_status, project_path),
        asyncio.to_thread(get_file_changes, project_path),
        asyncio.to_thread(get_git_diff_summary, 8000, project_path),
    )
    unresolved_errors = await asyncio.to_thread(
        load_open_tool_errors,
        getattr(session_manager, "db", None),
        session_id,
    )

    # Use digest as structured context if available (cheaper than transcript analysis)
    digest_markdown = getattr(current_session, "digest_markdown", None)
    if isinstance(digest_markdown, str) and digest_markdown.strip():
        bounded_digest = _truncate_markdown(digest_markdown, DIGEST_FALLBACK_MAX_CHARS)
        structured_context = f"Session Digest:\n{bounded_digest}"
        real_commits = await asyncio.to_thread(get_recent_git_commits, 10, project_path)
        if real_commits:
            commit_lines = [
                f"  - {c.get('hash', '')[:7]} {c.get('message', '')}" for c in real_commits[:10]
            ]
            structured_context += "\n\nRecent Commits:\n" + "\n".join(commit_lines)
    else:
        # Fallback: full transcript analysis
        from gobby.sessions.analyzer import TranscriptAnalyzer

        analyzer = TranscriptAnalyzer()
        handoff_ctx = analyzer.extract_handoff_context(turns, max_turns=150)
        real_commits = await asyncio.to_thread(get_recent_git_commits, 10, project_path)
        if real_commits:
            handoff_ctx.git_commits = real_commits
        if not handoff_ctx.git_status:
            handoff_ctx.git_status = git_status
        structured_context = _format_structured_context(handoff_ctx)

    if unresolved_errors:
        unresolved_errors_block = _truncate_markdown(
            format_unresolved_errors(unresolved_errors),
            TRANSCRIPT_FALLBACK_MAX_CHARS,
        )[:TRANSCRIPT_FALLBACK_MAX_CHARS]
        base_budget = max(
            0,
            TRANSCRIPT_FALLBACK_MAX_CHARS - len(unresolved_errors_block) - 2,
        )
        structured_context = _truncate_markdown(
            structured_context,
            base_budget,
        )[:base_budget]
        structured_context = (
            f"{structured_context}\n\n{unresolved_errors_block}"
            if structured_context
            else unresolved_errors_block
        )
    else:
        structured_context = _truncate_markdown(
            structured_context,
            TRANSCRIPT_FALLBACK_MAX_CHARS,
        )

    # 3. Call LLM
    try:
        llm_context = {
            "turns": recent_turns[-TRANSCRIPT_FALLBACK_MAX_TURNS:],
            "transcript_summary": transcript_summary,
            "session": current_session,
            "last_messages": last_messages_str,
            "git_status": git_status,
            "file_changes": file_changes,
            "git_diff_summary": git_diff_summary,
            "structured_context": structured_context,
            "todo_list": "",
            "previous_summary": previous_summary or "",
            "mode": mode,
        }
        from gobby.llm.prompt_rendering import render_summary_prompt

        prompt = render_summary_prompt(template, llm_context)
        summary_content = await llm_service.call_feature(
            session_summary_config,
            prompt,
            system_prompt=(
                "You are a session summary generator. Create comprehensive, actionable summaries."
            ),
            caller="workflows.generate_summary",
            output_validator=summary_markdown_validation_error,
        )
    except Exception as e:
        logger.error(
            "LLM generation failed",
            extra={"session_id": session_id, "error": str(e)},
        )
        return {"error": f"LLM error: {e}"}

    validation_error = summary_markdown_validation_error(summary_content)
    if validation_error is not None:
        logger.warning(
            "LLM returned invalid summary for session %s: %s",
            session_id,
            validation_error,
        )
        return {"error": f"LLM returned invalid summary: {validation_error}"}

    # 4. Save to session
    await asyncio.to_thread(
        session_manager.update_summary,
        session_id,
        summary_markdown=summary_content,
    )

    # 5. Write to file if requested
    summary_file_path = None
    if write_file:
        summary_file_path = await _write_summary_file(
            session_id=session_id,
            content=summary_content,
            output_path=output_path,
            session_manager=session_manager,
            mode=mode,
        )

    logger.debug(
        "Generated summary for session %s",
        session_id,
        extra={
            "mode": mode,
            "reason": "workflow_action",
            "output_chars": len(summary_content),
        },
    )
    result: dict[str, Any] = {"summary_generated": True, "summary_length": len(summary_content)}
    if summary_file_path:
        result["summary_file"] = summary_file_path
    return result
