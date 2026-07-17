"""Summary generation workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These functions handle session summary generation, title synthesis, and handoff creation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiofiles

from gobby.hooks.background_tasks import create_background_task
from gobby.memory.title_heuristics import normalize_title_candidate
from gobby.sessions.summary_context import load_summary_prompt_template
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
from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value
from gobby.sessions.workspace_context import resolve_session_workspace
from gobby.workflows.git_utils import (
    get_file_changes,
    get_git_diff_summary,
    get_git_status,
    get_recent_git_commits,
)

if TYPE_CHECKING:
    from gobby.config.sessions import SessionSummaryConfig
    from gobby.sessions.analyzer import HandoffContext

logger = logging.getLogger(__name__)

_UNRESOLVED_SESSION_REF_RE = re.compile(
    r"(?<![a-z0-9_])(?:#session_ref|#\{session_ref\}|\{session_ref\})(?![a-z0-9_])"
)


def _get_result_truncation_limit(content_str: str) -> int:
    """Return truncation limit based on content type.

    Errors/test output get 1000 chars for visibility. Default: 200 chars.
    """
    error_indicators = [
        "Error",
        "error",
        "ERROR",
        "Failed",
        "failed",
        "Traceback",
        "Exception",
        "FAIL",
        "AssertionError",
    ]
    if any(ind in content_str[:500] for ind in error_indicators):
        return 1000
    test_indicators = ["pytest", "PASSED", "FAILED", "test_", "npm test"]
    if any(ind in content_str[:500] for ind in test_indicators):
        return 1000
    return 200


def format_turns_for_llm(turns: list[dict[str, Any]]) -> str:
    """Format transcript turns for LLM analysis.

    Handles both Claude Code format (nested message.role/content) and typed
    JSON format (flat type/role/content).

    Args:
        turns: List of transcript turn dicts

    Returns:
        Formatted string with turn summaries
    """
    formatted: list[str] = []
    for i, turn in enumerate(turns):
        # Detect format: typed JSON uses "type" field, Claude uses nested "message"
        event_type = turn.get("type")

        if event_type:
            # Typed JSON format: flat structure with type field
            role, content = _format_typed_json_turn(turn, event_type)
            if role is None:
                continue  # Skip non-displayable events
        else:
            # Claude Code format: nested message structure
            role, content = _format_claude_turn(turn)

        formatted.append(f"[Turn {i + 1} - {role}]: {content}")

    return "\n\n".join(formatted)


def _format_typed_json_turn(turn: dict[str, Any], event_type: str) -> tuple[str | None, str]:
    """Format a typed-JSON transcript turn.

    Returns:
        Tuple of (role, formatted_content) or (None, "") if should skip
    """
    if event_type == "message":
        role = turn.get("role", "unknown")
        if role == "model":
            role = "assistant"
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        return role, str(content)

    elif event_type == "tool_use":
        tool_name = turn.get("tool_name") or turn.get("function_name", "unknown")
        params = turn.get("parameters") or turn.get("args", {})
        param_preview = str(params)[:100] if params else ""
        return "assistant", f"[Tool: {tool_name}] {param_preview}"

    elif event_type == "tool_result":
        tool_name = turn.get("tool_name", "")
        output = turn.get("output") or turn.get("result", "")
        output_str = str(output)
        limit = _get_result_truncation_limit(output_str)
        preview = output_str[:limit]
        suffix = "..." if len(output_str) > limit else ""
        return "tool", f"[Result{' from ' + tool_name if tool_name else ''}]: {preview}{suffix}"

    elif event_type in ("init", "result"):
        # Skip initialization and final result events
        return None, ""

    else:
        # Unknown type, try to extract something
        content = turn.get("content", turn.get("message", ""))
        return "unknown", str(content)[:200]


def _format_claude_turn(turn: dict[str, Any]) -> tuple[str, str]:
    """Format a Claude Code turn with nested message structure."""
    message = turn.get("message", {})
    role = message.get("role", "unknown")
    content = message.get("content", "")

    # Assistant messages have content as array of blocks
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "thinking":
                    text_parts.append(f"[Thinking: {block.get('thinking', '')}]")
                elif block.get("type") == "tool_use":
                    text_parts.append(f"[Tool: {block.get('name', 'unknown')}]")
                elif block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    # Extract text from list of content blocks if needed
                    if isinstance(result_content, list):
                        extracted = []
                        for item in result_content:
                            if isinstance(item, dict):
                                extracted.append(item.get("text", "") or item.get("content", ""))
                            else:
                                extracted.append(str(item))
                        result_content = " ".join(extracted)
                    content_str = str(result_content)
                    limit = _get_result_truncation_limit(content_str)
                    preview = content_str[:limit]
                    suffix = "..." if len(content_str) > limit else ""
                    text_parts.append(f"[Result: {preview}{suffix}]")
        content = " ".join(text_parts)

    return role, str(content)


def _format_structured_context(ctx: HandoffContext) -> str:
    """Format HandoffContext fields as concise text for LLM consumption.

    Args:
        ctx: Structured context extracted from transcript analysis

    Returns:
        Formatted text block with anchoring data (files, commits, decisions)
    """
    sections: list[str] = []

    if ctx.active_gobby_task:
        task = ctx.active_gobby_task
        if isinstance(task, dict):
            sections.append(
                f"Active Task: {task.get('title', 'Untitled')} "
                f"(#{task.get('id', '?')}, status: {task.get('status', 'unknown')})"
            )
        else:
            sections.append(f"Active Task: {task}")

    if ctx.task_progress:
        progress_lines = []
        for p in ctx.task_progress[-15:]:
            if isinstance(p, dict):
                progress_lines.append(
                    f"  - {p.get('action', '?')}: {p.get('title', '?')} ({p.get('id', '?')})"
                )
            else:
                progress_lines.append(f"  - {p}")
        sections.append("Task Progress:\n" + "\n".join(progress_lines))

    if ctx.initial_goal:
        sections.append(f"Original Goal: {ctx.initial_goal[:500]}")

    if ctx.files_modified:
        sections.append("Files Modified:\n" + "\n".join(f"  - {f}" for f in ctx.files_modified))

    if ctx.git_commits:
        commit_lines = []
        for c in ctx.git_commits[:10]:
            if isinstance(c, dict):
                commit_lines.append(f"  - {c.get('hash', '')[:7]} {c.get('message', '')}")
            else:
                commit_lines.append(f"  - {c}")
        sections.append("Recent Commits:\n" + "\n".join(commit_lines))

    if ctx.recent_activity:
        sections.append(
            "Recent Activity:\n" + "\n".join(f"  - {a}" for a in ctx.recent_activity[-10:])
        )

    if ctx.key_decisions:
        sections.append("Key Decisions:\n" + "\n".join(f"  - {d}" for d in ctx.key_decisions))

    return "\n\n".join(sections) if sections else ""


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

        logger.info(
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
        logger.error(
            f"Failed to write summary file: {e}",
            exc_info=True,
            extra={
                "session_id": session_id,
                "ref": ref,
                "output_dir": str(summary_dir) if summary_dir is not None else None,
            },
        )
        return None


def schedule_tmux_window_rename(
    session: Any,
    title: str,
    *,
    loop: Any | None = None,
) -> None:
    """Run ``_rename_tmux_window`` from sync code using the best available loop."""
    import asyncio

    coro = _rename_tmux_window(session, title)

    try:
        running_loop = asyncio.get_running_loop()
        create_background_task(coro, loop=running_loop)
        return
    except RuntimeError:
        pass

    if loop is not None:
        try:
            loop_is_usable = not loop.is_closed()
        except Exception:
            loop_is_usable = False

        if loop_is_usable:
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
                return
            except Exception:
                logger.debug("Failed to schedule tmux rename on captured loop", exc_info=True)
                coro.close()
                return

    try:
        asyncio.run(coro)
    except Exception:
        logger.debug("Failed to run tmux rename synchronously", exc_info=True)


def _synthesize_fallback_title(session: object, terminal_context: dict[str, Any]) -> str:
    """Synthesize a fallback title for a session that still has no title.

    Deliberately never derives from terminal paths (cwd / project_path /
    workspace_path / repo_path basename): a path basename is indistinguishable
    from a real title and is exactly what made title-less sessions masquerade as
    the project directory (the original ``#N gobby`` bug). Falls back to the
    session ``source`` (e.g. ``claude``), then a neutral ``"untitled"`` label.

    With the per-turn heuristic, digest-cancellation fallback, and repair-sweep
    title synthesis in place this branch is rarely reached; when it is, it must
    no longer produce a misleading directory name.

    Args:
        session: The session object
        terminal_context: Parsed terminal context dict (unused; retained for the
            stable call signature shared with ``_resolve_window_title``)

    Returns:
        A fallback title string
    """
    session_source = getattr(session, "source", None)
    if session_source:
        return str(session_source)

    return "untitled"


def _contains_unresolved_session_ref(value: Any) -> bool:
    return isinstance(value, str) and _UNRESOLVED_SESSION_REF_RE.search(value.lower()) is not None


def _strip_window_ref_prefix(title: str, ref: str | None) -> str:
    title = title.strip()
    if not ref:
        return title
    if title == ref:
        return ""
    prefix_re = re.compile(rf"^(?:{re.escape(ref)}(?::\s*|\s+))+")
    return prefix_re.sub("", title).strip()


def _sanitize_tmux_window_title(value: str) -> str:
    """Return a tmux-safe window title while preserving readable punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"\s*:\s*", " - ", value)).strip()


def _session_ref_for_window_title(session: Any) -> str | None:
    seq_num = getattr(session, "seq_num", None)
    if isinstance(seq_num, int) and seq_num > 0:
        return f"#{seq_num}"

    ref = getattr(session, "ref", None)
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref or _contains_unresolved_session_ref(ref):
        return None
    return ref


def _resolve_window_title(session: Any, terminal_context: dict[str, Any], title: str) -> str:
    """Resolve the final tmux window title: fallback when empty, ref-prefixed.

    Prepends the session ref (e.g. ``#3605``) so the window reads ``#N title``.
    """
    ref = _session_ref_for_window_title(session)
    if not title or _contains_unresolved_session_ref(title):
        title = _synthesize_fallback_title(session, terminal_context)
    title = _strip_window_ref_prefix(str(title), ref)
    resolved_title = normalize_title_candidate(title)
    if not resolved_title:
        fallback_title = _strip_window_ref_prefix(
            _synthesize_fallback_title(session, terminal_context),
            ref,
        )
        resolved_title = normalize_title_candidate(fallback_title) or ""
    resolved_title = _sanitize_tmux_window_title(resolved_title)
    if ref:
        return f"{ref} {resolved_title}".strip()
    return resolved_title


def _tmux_manager_for_session(session: Any, terminal_context: dict[str, Any]) -> Any:
    """Build a tmux manager for *session*'s recorded server context."""
    agent_depth = getattr(session, "agent_depth", 0) or 0
    default_socket_name = "gobby" if agent_depth > 0 else ""
    return get_tmux_manager_for_context(terminal_context, default_socket_name=default_socket_name)


async def _apply_window_rename(
    session: Any, terminal_context: dict[str, Any], pane: str, title: str
) -> bool:
    """Rename *pane*'s window for *session*, logging the structured outcome.

    Failures are logged but never propagated. Returns True only when tmux
    confirms the rename was applied.
    """
    resolved = _resolve_window_title(session, terminal_context, title)
    ref = getattr(session, "ref", "?")
    socket = (
        terminal_context.get("tmux_socket_path")
        or terminal_context.get("tmux_socket_name")
        or "default"
    )
    try:
        mgr = _tmux_manager_for_session(session, terminal_context)
        applied = bool(await mgr.rename_window(pane, resolved))
    except Exception as e:
        logger.debug(
            "tmux window rename errored for %s pane=%s socket=%s title=%r: %s",
            ref,
            pane,
            socket,
            resolved,
            e,
        )
        return False
    if applied:
        logger.info(
            "Renamed tmux window for %s pane=%s socket=%s title=%r",
            ref,
            pane,
            socket,
            resolved,
        )
    else:
        logger.debug(
            "tmux window rename did not apply for %s pane=%s socket=%s title=%r "
            "(target missing or tmux error)",
            ref,
            pane,
            socket,
            resolved,
        )
    return applied


async def _managed_window_name_needs_repair(
    mgr: Any,
    pane: str,
    session: Any,
    terminal_context: dict[str, Any],
) -> bool:
    getter = getattr(mgr, "get_window_name", None)
    if getter is None:
        return False
    try:
        window_name = await getter(pane)
    except Exception:
        logger.debug("Failed to read window name for pane %s", pane, exc_info=True)
        return False
    if _contains_unresolved_session_ref(window_name):
        return True
    if not isinstance(window_name, str):
        return False
    current = window_name.strip()
    if not current:
        return False
    return _resolve_window_title(session, terminal_context, current) != current


async def _rename_tmux_window(session: Any, title: str) -> None:
    """Rename the tmux window for a session after title synthesis.

    Uses the tmux server recorded in terminal context when present. Falls back
    to the default user server for user sessions and Gobby's isolated socket for
    spawned agents.
    Failures are logged but never propagated.
    """
    tc = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not tc:
        return
    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return
    await _apply_window_rename(session, tc, pane, title)


async def enforce_window_name_if_unmanaged(session: Any) -> bool:
    """Rename a tracked session's tmux window when unmanaged or visibly stale.

    Used by the periodic repair sweep. A window Gobby has already named has
    ``automatic-rename`` off (``rename_window`` disables it); such windows are
    left untouched unless their current title still normalizes to a different
    Gobby-owned title. Returns True when a rename was issued.

    This is the durable safety net for sessions whose session-start rename never
    lands — notably interactive Claude sessions in a VSCode tmux pane, which keep
    an empty title and otherwise stay frozen on the CLI's startup OSC window name
    (e.g. its version string).

    Returns False when terminal context is missing, the tmux pane is absent, the
    window cannot be inspected, or the window is already managed and has no
    unresolved session placeholder.
    """
    tc = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not tc:
        return False
    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return False

    mgr = _tmux_manager_for_session(session, tc)
    try:
        auto_rename = await mgr.get_window_automatic_rename(pane)
    except Exception:
        logger.debug("Failed to read automatic-rename for pane %s", pane, exc_info=True)
        return False
    # None -> window unreadable/gone; False -> already Gobby-managed. Bad names
    # from older builds are repaired even though they are already managed.
    if auto_rename is None:
        return False
    if auto_rename is False and not await _managed_window_name_needs_repair(mgr, pane, session, tc):
        return False

    title = getattr(session, "title", None) or ""
    return await _apply_window_rename(session, tc, pane, title)


async def repair_missing_session_title(session_manager: Any, session: Any) -> str | None:
    """Synthesize and persist a heuristic title for a title-less or provisional session.

    The provider-agnostic backstop for the repair sweep: when a tracked session
    still carries no real title — because the per-turn heuristic and LLM digest
    paths both missed (e.g. a session interrupted before ``turn_end``, or one
    whose hooks were mis-routed before the routing fix) — derive a cheap title
    from the transcript's opening user prompt (no LLM) and persist it with
    ``title_source="heuristic"``.

    The transcript is the guard, not a DB stat. ``heuristic_title_from_transcript``
    returns ``None`` when there is no usable opening prompt, so the backstop stays
    robust even when ``turn_count`` lags at 0: a session's title comes from its
    first user prompt, which can exist with ``turn_count == 0`` (assistant
    mid-turn, or only tool-use/thinking blocks so far). Gating on ``turn_count``
    would skip exactly those sessions.

    Persisting routes through ``session_manager.update_title``, whose
    title-change side effects schedule the tmux window rename, so the window
    stops showing the empty/provisional fallback. Returns the persisted title,
    or ``None`` when no synthesis was applicable (an existing non-provisional or
    manual title, a missing session id, or no usable transcript prompt).
    """
    if not session_manager or session is None:
        return None
    title_source = str(getattr(session, "title_source", "") or "").strip().lower()
    existing_title = str(getattr(session, "title", "") or "").strip()
    if title_source == "manual":
        return None
    if existing_title and title_source != "provisional":
        return None

    session_id = getattr(session, "id", None)
    if not session_id:
        return None

    from gobby.memory.title_heuristics import heuristic_title_from_transcript

    title = await heuristic_title_from_transcript(
        getattr(session, "transcript_path", None),
        getattr(session, "source", None),
    )
    if not title:
        return None

    updated = session_manager.update_title(session_id, title, title_source="heuristic")
    if updated is None:
        return None
    logger.info(
        "Repair sweep synthesized heuristic title for session %s",
        getattr(session, "ref", session_id),
    )
    return title


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

    transcript_path = getattr(current_session, "transcript_path", None)
    if not transcript_path:
        logger.warning(f"generate_summary: No transcript path for session {session_id}")
        return {"error": "No transcript path"}

    if not template:
        template = await asyncio.to_thread(
            load_summary_prompt_template,
            path="handoff/session_end",
            session_summary_config=session_summary_config,
            db=getattr(session_manager, "db", None),
            session_manager=session_manager,
            allow_runtime_db=False,
        )

    prompt_error = summary_prompt_validation_error(template)
    if prompt_error is not None:
        logger.warning(
            "Invalid on-demand summary prompt",
            extra={"session_id": session_id, "error": prompt_error},
        )
        return {"error": f"Invalid summary prompt template: {prompt_error}"}
    assert isinstance(template, str)

    # 1. Process Transcript
    try:
        transcript_file = Path(transcript_path)
        if not transcript_file.exists():
            logger.warning(f"Transcript file not found: {transcript_path}")
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
        logger.error(f"Failed to process transcript: {e}")
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

    logger.info(f"Generated summary for session {session_id} (mode={mode})")
    result: dict[str, Any] = {"summary_generated": True, "summary_length": len(summary_content)}
    if summary_file_path:
        result["summary_file"] = summary_file_path
    return result
