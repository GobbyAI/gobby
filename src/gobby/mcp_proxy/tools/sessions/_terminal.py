"""Terminal interaction tools for tmux-backed sessions.

Exposes send_keys, capture_output, and compact_self as MCP tools on
gobby-sessions, enabling orchestration (heartbeat, pipelines, other agents)
to interact with running terminal sessions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.sessions.compact_continuation import (
    build_compact_self_continue_prompt,
    clear_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
    persist_compact_resume_required_skills,
    schedule_compact_self_continuation_fallback,
)
from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value
from gobby.storage.agents import LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)
_TRANSCRIPT_TAIL_MAX_BYTES = 256 * 1024


def _read_transcript_tail_lines(path: Path, max_lines: int) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - _TRANSCRIPT_TAIL_MAX_BYTES)
        handle.seek(start)
        data = handle.read()

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and len(lines) > 1:
        lines = lines[1:]
    return lines[-max_lines:]


# Maps session.source (CLI provider name) to the slash command that triggers
# context compaction in the running CLI subprocess.
_CLI_COMPACT_COMMANDS: dict[str, str] = {
    "claude": "/compact",
    "codex": "/compact",
    "gemini": "/compress",
    "qwen": "/compress",
    "droid": "/compress",
}
_CODEX_INTERRUPT_KEY = "Escape"
_CODEX_INTERRUPT_SETTLE_SECONDS = 0.2
_DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS = 10.0
_COMPACT_HANDOFF_FALLBACK_MAX_CHARS = 20_000


async def _send_tmux_keys(
    tmux: TmuxSessionManager,
    target: str,
    keys: str,
    session_id: str,
    *,
    literal: bool,
    action: str,
) -> tuple[bool, str | None]:
    """Send tmux keys and keep failures structured for MCP callers."""
    try:
        ok = await tmux.send_keys(target, keys, literal=literal)
    except TimeoutError:
        logger.warning("Timed out %s to tmux target %s for %s", action, target, session_id)
        return False, f"tmux send-keys timed out for session {session_id} while {action}"
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        logger.warning(
            "Failed %s to tmux target %s for %s: %s",
            action,
            target,
            session_id,
            detail,
            exc_info=True,
        )
        return False, f"tmux send-keys failed for session {session_id} while {action}: {detail}"

    if not ok:
        return False, f"tmux send-keys failed for session {session_id} while {action}"
    return True, None


async def _send_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
) -> tuple[bool, str | None]:
    """Send a compaction command through tmux and keep failures structured."""
    return await _send_tmux_keys(
        tmux,
        target,
        f"{command}\n",
        session_id,
        literal=True,
        action="sending compaction command",
    )


async def _send_codex_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
    *,
    settle_seconds: float | None = None,
) -> tuple[bool, str | None]:
    """Backward-compatible wrapper for tests around the terminal compaction flow."""
    ok, reason, _continuation_pending = await _send_terminal_compaction_command(
        tmux,
        target,
        command,
        session_id,
        mark_continuation_pending=lambda: False,
        clear_continuation_pending=lambda: False,
        settle_seconds=settle_seconds,
    )
    return ok, reason


async def _send_terminal_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
    *,
    mark_continuation_pending: Callable[[], bool],
    clear_continuation_pending: Callable[[], bool],
    settle_seconds: float | None = None,
) -> tuple[bool, str | None, bool]:
    """Interrupt the active prompt, mark continuation pending, then compact."""
    ok, reason = await _send_tmux_keys(
        tmux,
        target,
        _CODEX_INTERRUPT_KEY,
        session_id,
        literal=False,
        action="sending compaction interrupt",
    )
    if not ok:
        return False, reason, False

    delay = _CODEX_INTERRUPT_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    if delay > 0:
        await asyncio.sleep(delay)

    continuation_pending = bool(mark_continuation_pending())
    ok, reason = await _send_compaction_command(tmux, target, command, session_id)
    if not ok:
        if continuation_pending:
            clear_continuation_pending()
        return False, reason, False
    return True, None, continuation_pending


def _resolve_tmux_target(
    session_id: str,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
) -> tuple[str | None, TmuxSessionManager | None, str | None]:
    """Resolve a session ID to a tmux target.

    Returns:
        (tmux_target, tmux_manager, error_message).
    """
    # Try agent run first (agent sessions have tmux_session_name on the run)
    agent_run = agent_run_manager.get_by_session(session_id)
    if agent_run is not None:
        if agent_run.status not in ("running", "pending"):
            return None, None, f"Agent session is not running (status={agent_run.status})"
        if not agent_run.tmux_session_name:
            return None, None, "Agent session has no tmux terminal (mode may be autonomous)"
        return agent_run.tmux_session_name, TmuxSessionManager(TmuxConfig()), None

    # Fallback: interactive CLI session with terminal_context
    session = session_manager.get(session_id)
    if session is None:
        return None, None, f"Session {session_id} not found"

    if session.terminal_context:
        ctx = parse_terminal_context_value(session.terminal_context)
        if ctx is None:
            raw_type = type(session.terminal_context).__name__
            return (
                None,
                None,
                f"Session {session_id} has invalid terminal_context ({raw_type}); "
                "expected object or JSON object",
            )
        # terminal_context may contain tmux_pane or tmux_session
        tmux_target = ctx.get("tmux_pane") or ctx.get("tmux_session")
        if tmux_target:
            return tmux_target, get_tmux_manager_for_context(ctx), None
        keys = ", ".join(sorted(str(key) for key in ctx.keys())) or "none"
        return (
            None,
            None,
            f"Session {session_id} terminal_context has no tmux_pane or tmux_session "
            f"(keys: {keys})",
        )

    return None, None, f"Session {session_id} has no tmux terminal"


def _resolve_session_for_compaction(
    session_id: str,
    session_manager: SessionManager,
) -> tuple[str | None, Any | None, str | None]:
    """Resolve a user-facing session ref for compact_self."""
    resolved_id = session_id
    resolver = getattr(session_manager, "resolve_session_reference", None)
    if callable(resolver):
        try:
            from gobby.utils.project_context import get_project_context

            project_ctx = get_project_context()
            project_id = project_ctx.get("id") if project_ctx else None
            candidate = resolver(session_id, project_id)
            if isinstance(candidate, str) and candidate:
                resolved_id = candidate
        except ValueError as exc:
            return None, None, f"Session {session_id} not found: {exc}"
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.warning(
                "Failed resolving session reference %r for compaction: %s",
                session_id,
                detail,
                exc_info=True,
            )
            return None, None, f"failed to resolve session {session_id}: {detail}"

    session = session_manager.get(resolved_id)
    if session is None:
        return resolved_id, None, f"Session {session_id} not found"
    return resolved_id, session, None


def _has_summary_refresh_source(session: Any) -> bool:
    """Return whether summary generation has current session content to read."""
    digest_markdown = getattr(session, "digest_markdown", None)
    if isinstance(digest_markdown, str) and digest_markdown.strip():
        return True

    transcript_path = getattr(session, "transcript_path", None)
    return isinstance(transcript_path, str) and bool(transcript_path.strip())


def _compact_handoff_refresh_timeout_seconds() -> float:
    try:
        from gobby.config.app import load_config
    except ImportError as exc:
        logger.debug("Using default compact handoff refresh timeout: %s", exc)
        return _DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS

    config = load_config()
    compact_handoff = getattr(config, "compact_handoff", None)
    value = getattr(
        compact_handoff,
        "refresh_timeout_seconds",
        _DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS,
    )
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        logger.debug("Using default compact handoff refresh timeout: %s", exc)
        return _DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS


def _compact_handoff_fallback_markdown(session: Any, *, reason: str) -> str | None:
    """Build a bounded handoff fallback when LLM summary refresh cannot finish."""
    digest_markdown = getattr(session, "digest_markdown", None)
    if isinstance(digest_markdown, str) and digest_markdown.strip():
        digest = digest_markdown.strip()
        if len(digest) > _COMPACT_HANDOFF_FALLBACK_MAX_CHARS:
            digest = digest[-_COMPACT_HANDOFF_FALLBACK_MAX_CHARS:].lstrip()
            digest = "[older digest content truncated]\n\n" + digest
        return (
            "# Compact Handoff\n\n"
            f"LLM handoff refresh did not complete before compaction ({reason}). "
            "Continuing with the latest session digest.\n\n"
            f"{digest}"
        )

    summary_markdown = getattr(session, "summary_markdown", None)
    if isinstance(summary_markdown, str) and summary_markdown.strip():
        return summary_markdown.strip()
    return None


async def _persist_compact_handoff_fallback(
    session_id: str,
    session: Any,
    session_manager: SessionManager,
    *,
    reason: str,
) -> dict[str, Any]:
    fallback = _compact_handoff_fallback_markdown(session, reason=reason)
    if not fallback:
        return {
            "success": False,
            "error": f"handoff refresh {reason} and no digest/summary fallback exists",
            "timed_out": reason == "timed out",
        }

    try:
        session_manager.update_summary(session_id, summary_markdown=fallback)
        session_manager.update_status(session_id, "handoff_ready")
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        logger.warning(
            "Failed persisting compact_self handoff fallback for %s: %s",
            session_id,
            detail,
            exc_info=True,
        )
        return {"success": False, "error": detail, "timed_out": reason == "timed out"}

    return {
        "success": True,
        "refreshed": True,
        "fallback": True,
        "timed_out": reason == "timed out",
        "summary_length": len(fallback),
    }


async def _refresh_compact_handoff_context(
    session_id: str,
    session: Any,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any | None,
) -> dict[str, Any]:
    """Refresh summary_markdown before compact_self can trigger same-session resume."""
    if not _has_summary_refresh_source(session):
        return {"success": True, "refreshed": False, "reason": "no_summary_refresh_source"}

    from gobby.sessions.summarize import generate_session_summaries

    timeout_seconds = _compact_handoff_refresh_timeout_seconds()
    try:
        result = await asyncio.wait_for(
            generate_session_summaries(
                session_id=session_id,
                session_manager=session_manager,
                llm_service=llm_service,
                db=db,
                set_handoff_ready=True,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Timed out refreshing compact_self handoff context for %s after %.1fs; "
            "using digest fallback",
            session_id,
            timeout_seconds,
        )
        return await _persist_compact_handoff_fallback(
            session_id,
            session,
            session_manager,
            reason="timed out",
        )
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        logger.warning(
            "Failed refreshing compact_self handoff context for %s: %s",
            session_id,
            detail,
            exc_info=True,
        )
        return {"success": False, "error": detail}

    if not result.get("success"):
        error = str(result.get("error") or result.get("full_error") or "unknown error")
        return {"success": False, "error": error}

    refreshed_session = session_manager.get(session_id)
    summary_markdown = getattr(refreshed_session, "summary_markdown", None)
    if not isinstance(summary_markdown, str) or not summary_markdown.strip():
        return {"success": False, "error": "summary refresh produced no summary_markdown"}

    return {
        "success": True,
        "refreshed": True,
        "summary_length": len(summary_markdown),
    }


async def _capture_transcript_tail(
    session_id: str,
    session_manager: SessionManager,
    lines: int,
    *,
    tmux_error: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return transcript tail fallback when no live tmux target is available."""
    session = session_manager.get(session_id)
    if session is None:
        return None, "session_not_found"

    transcript_path = getattr(session, "transcript_path", None)
    if not isinstance(transcript_path, str) or not transcript_path:
        return None, "missing_transcript_path"

    path = Path(transcript_path)
    if not path.is_file():
        return None, "transcript_not_found"

    max_lines = max(1, lines)
    try:
        tail = await asyncio.to_thread(_read_transcript_tail_lines, path, max_lines)
    except OSError as exc:
        detail = str(exc) or type(exc).__name__
        return None, f"transcript_read_failed: {detail}"

    return (
        {
            "success": True,
            "output": "\n".join(tail),
            "via": "transcript",
            "transcript_path": transcript_path,
            "note": "No live tmux pane was capturable; returned transcript tail instead.",
            "tmux_error": tmux_error,
        },
        None,
    )


async def _compact_live_web_chat_fallback(
    web_chat_session_registry: WebChatSessionRegistry | None,
    *session_ids: str | None,
) -> dict[str, Any] | None:
    """Compact a live web-chat session when DB-backed session lookup is unavailable."""
    if web_chat_session_registry is None:
        return None

    seen: set[str] = set()
    for session_id in session_ids:
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        try:
            live_session = web_chat_session_registry.find_session(session_id)[1]
        except (LookupError, KeyError, RuntimeError):
            logger.debug(
                "Failed to look up live web_chat session %s for compaction fallback",
                session_id,
                exc_info=True,
            )
            continue
        if live_session is None:
            continue
        try:
            return await web_chat_session_registry.compact_session(session_id)
        except (LookupError, KeyError, RuntimeError):
            logger.warning(
                "Failed to compact live web_chat session %s via fallback",
                session_id,
                exc_info=True,
            )
            continue
    return None


def register_terminal_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any | None = None,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
) -> None:
    """Register send_keys and capture_output tools."""

    agent_run_manager = LocalAgentRunManager(db)

    @registry.tool(
        name="send_keys",
        description=(
            "Send keystrokes to a session's tmux terminal. "
            "Use literal=true (default) to type text — trailing \\n sends Enter. "
            "Use literal=false for tmux key names: C-c, Escape, Enter, C-d."
        ),
    )
    async def send_keys(
        session_id: str,
        keys: str,
        literal: bool = True,
    ) -> dict[str, Any]:
        target, tmux, error = _resolve_tmux_target(session_id, session_manager, agent_run_manager)
        if error:
            return {"success": False, "error": error}

        assert target is not None
        assert tmux is not None
        ok = await tmux.send_keys(target, keys, literal=literal)
        if not ok:
            return {
                "success": False,
                "error": f"tmux send-keys failed for session {session_id}",
            }
        return {"success": True}

    @registry.tool(
        name="compact_self",
        description=(
            "Trigger context compaction in the current MCP caller's CLI by firing "
            "the appropriate slash command (/compact for Claude Code, "
            "/compact for Codex, /compress for other supported CLIs). Designed "
            "to be called at workflow handoff boundaries — e.g. /gobby plan calls this after spawning "
            "plan-adversary so the coordinator's bulky requirements-gathering "
            "context is summarized away while the sub-agent runs. Web-chat "
            "sessions use the live daemon ChatSession registry."
        ),
    )
    async def compact_self(rule_name: str | None = None) -> dict[str, Any]:
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {
                "compacted": False,
                "reason": "compact_self requires current MCP SessionContext",
            }
        if rule_name:
            logger.info("Compacting session %s (triggered by rule %s)", session_id, rule_name)
        resolved_session_id, session, error = _resolve_session_for_compaction(
            session_id,
            session_manager,
        )
        if error:
            web_chat_fallback = await _compact_live_web_chat_fallback(
                web_chat_session_registry,
                session_id,
                resolved_session_id,
            )
            if web_chat_fallback is not None:
                return web_chat_fallback
            return {"compacted": False, "reason": error}
        assert resolved_session_id is not None
        assert session is not None

        session_type = getattr(session, "session_type", "terminal")
        source = getattr(session, "source", None)

        if session_type == "web_chat":
            if web_chat_session_registry is None:
                return {
                    "compacted": False,
                    "reason": "web_chat session registry is not available",
                }
            if (
                resolved_session_id != session_id
                and web_chat_session_registry.find_session(resolved_session_id)[1] is None
            ):
                return await web_chat_session_registry.compact_session(session_id)
            return await web_chat_session_registry.compact_session(resolved_session_id)

        if session_type != "terminal":
            return {
                "compacted": False,
                "reason": f"unsupported session_type: {session_type}",
            }

        command = _CLI_COMPACT_COMMANDS.get(source) if source else None
        if command is None:
            return {
                "compacted": False,
                "reason": f"no compaction command known for cli={source!r}",
            }

        target, tmux, error = _resolve_tmux_target(
            resolved_session_id,
            session_manager,
            agent_run_manager,
        )
        if error:
            return {"compacted": False, "reason": error}
        assert target is not None
        assert tmux is not None

        refresh_result = await _refresh_compact_handoff_context(
            resolved_session_id,
            session,
            session_manager,
            db,
            llm_service,
        )
        if not refresh_result.get("success"):
            return {
                "compacted": False,
                "reason": "handoff context refresh failed before compaction: "
                f"{refresh_result.get('error', 'unknown error')}",
            }

        required_skills = persist_compact_resume_required_skills(db, resolved_session_id)
        continuation_prompt = build_compact_self_continue_prompt(required_skills)
        ok, reason, continuation_pending = await _send_terminal_compaction_command(
            tmux,
            target,
            command,
            resolved_session_id,
            mark_continuation_pending=lambda: mark_compact_self_continuation_pending(
                db,
                resolved_session_id,
                prompt=continuation_prompt,
            ),
            clear_continuation_pending=lambda: clear_compact_self_continuation_pending(
                db,
                resolved_session_id,
            ),
        )

        if not ok:
            return {
                "compacted": False,
                "reason": reason,
            }
        if continuation_pending:
            schedule_compact_self_continuation_fallback(
                db,
                pending_session_id=resolved_session_id,
                target_session=session,
            )
        result = {
            "compacted": True,
            "command": command,
            "cli": source,
            "via": "tmux",
            "interrupted": True,
            "continuation_pending": continuation_pending,
        }
        if required_skills:
            result["compact_resume_required_skills"] = required_skills
        if refresh_result.get("refreshed"):
            result["handoff_context_refreshed"] = True
            result["handoff_summary_length"] = refresh_result.get("summary_length")
        if refresh_result.get("fallback"):
            result["handoff_context_fallback"] = True
        if refresh_result.get("timed_out"):
            result["handoff_context_refresh_timed_out"] = True
        return result

    @registry.tool(
        name="capture_output",
        description=(
            "Capture the last N lines of a session's tmux terminal output. "
            "Useful for inspecting permission dialogs, trust prompts, or "
            "other terminal state not visible through hooks."
        ),
    )
    async def capture_output(
        session_id: str,
        lines: int = 50,
    ) -> dict[str, Any]:
        target, tmux, error = _resolve_tmux_target(session_id, session_manager, agent_run_manager)
        if error:
            fallback, transcript_error = await _capture_transcript_tail(
                session_id,
                session_manager,
                lines,
                tmux_error=error,
            )
            if fallback is not None:
                return fallback
            return {
                "success": False,
                "error": error,
                "error_code": "no_live_pane_or_transcript",
                "tmux_error": error,
                "transcript_error": transcript_error,
            }

        assert target is not None
        assert tmux is not None
        output = await tmux.capture_pane(target, lines)
        if output is None:
            return {
                "success": False,
                "error": f"Failed to capture pane for session {session_id}",
            }
        return {"success": True, "output": output, "via": "tmux"}
