"""Compact handoff context helpers for terminal session tools."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.sessions._terminal_transcripts import _read_transcript_tail_lines

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

_DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS = 300.0
_COMPACT_HANDOFF_FALLBACK_MAX_CHARS = 20_000


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


def _compact_handoff_digest_fallback_markdown(session: Any, *, reason: str) -> str | None:
    """Build a bounded digest handoff fallback."""
    digest_markdown = getattr(session, "digest_markdown", None)
    if not isinstance(digest_markdown, str) or not digest_markdown.strip():
        return None

    digest = digest_markdown.strip()
    if len(digest) > _COMPACT_HANDOFF_FALLBACK_MAX_CHARS:
        digest = digest[-_COMPACT_HANDOFF_FALLBACK_MAX_CHARS:].lstrip()
        digest = "[older digest content truncated]\n\n" + digest
    return (
        "# Compact Handoff\n\n"
        f"Archival handoff refresh is running in the background ({reason}). "
        "Continuing with the latest session digest.\n\n"
        f"{digest}"
    )


async def _compact_handoff_transcript_tail_markdown(
    session: Any,
    *,
    reason: str,
) -> str | None:
    """Build a bounded transcript-tail fallback when no digest is available."""
    transcript_path = getattr(session, "transcript_path", None)
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return None

    path = Path(transcript_path)
    if not path.is_file():
        return None

    try:
        tail_lines = await asyncio.to_thread(_read_transcript_tail_lines, path, 80)
    except OSError as exc:
        logger.debug("Failed reading compact_self transcript tail for %s: %s", path, exc)
        return None

    tail = "\n".join(tail_lines).strip()
    if not tail:
        return None
    if len(tail) > _COMPACT_HANDOFF_FALLBACK_MAX_CHARS:
        tail = tail[-_COMPACT_HANDOFF_FALLBACK_MAX_CHARS:].lstrip()
        tail = "[older transcript content truncated]\n\n" + tail

    return (
        "# Compact Handoff\n\n"
        f"Archival handoff refresh is running in the background ({reason}). "
        "No digest was available, so this handoff uses a bounded transcript tail.\n\n"
        "```text\n"
        f"{tail}\n"
        "```"
    )


def _valid_existing_summary_markdown(session: Any) -> str | None:
    from gobby.sessions.summary_validity import is_summary_markdown_valid

    summary_markdown = getattr(session, "summary_markdown", None)
    if isinstance(summary_markdown, str) and is_summary_markdown_valid(summary_markdown):
        return summary_markdown.strip()
    return None


def _mark_compact_handoff_ready(
    session_id: str,
    session: Any,
    session_manager: SessionManager,
    *,
    fallback: bool,
) -> dict[str, Any]:
    summary_markdown = getattr(session, "summary_markdown", None)
    summary_length = len(summary_markdown) if isinstance(summary_markdown, str) else 0
    try:
        session_manager.update_status(session_id, "handoff_ready")
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        logger.warning(
            "Failed marking compact_self handoff ready for %s: %s",
            session_id,
            detail,
            exc_info=True,
        )
        return {"success": False, "error": detail}

    return {
        "success": True,
        "refreshed": False,
        "fallback": fallback,
        "summary_length": summary_length,
    }


async def _persist_compact_handoff_fallback(
    session_id: str,
    session: Any,
    session_manager: SessionManager,
    *,
    reason: str,
) -> dict[str, Any]:
    from gobby.sessions.summary_refresh import digest_turn_count

    fallback = _compact_handoff_digest_fallback_markdown(session, reason=reason)
    if fallback is None:
        fallback = await _compact_handoff_transcript_tail_markdown(session, reason=reason)
    if not fallback:
        return {
            "success": False,
            "error": f"handoff refresh {reason} and no digest/summary/transcript fallback exists",
        }

    try:
        persist_summary_state = getattr(session_manager, "persist_summary_state", None)
        if callable(persist_summary_state):
            persist_summary_state(
                session_id,
                summary_markdown=fallback,
                generation_mode="digest_fallback",
                source_context_hash=None,
                source_digest_turn_count=digest_turn_count(
                    getattr(session, "digest_markdown", None)
                ),
                metadata_json={"reason": reason, "source": "compact_self"},
            )
        else:
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
        return {"success": False, "error": detail}

    return {
        "success": True,
        "refreshed": True,
        "fallback": True,
        "summary_length": len(fallback),
        "background_refresh_needed": _has_summary_refresh_source(session),
    }


async def _run_compact_handoff_background_refresh(
    session_id: str,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any | None,
    session_summary_config: Any | None,
) -> None:
    from gobby.sessions.summarize import generate_session_summaries

    timeout_seconds = _compact_handoff_refresh_timeout_seconds()
    try:
        result = await asyncio.wait_for(
            generate_session_summaries(
                session_id=session_id,
                session_manager=session_manager,
                llm_service=llm_service,
                session_summary_config=session_summary_config,
                db=db,
                set_handoff_ready=False,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.debug(
            "Timed out refreshing compact_self archival handoff context for %s after %.1fs",
            session_id,
            timeout_seconds,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        logger.debug(
            "Failed refreshing compact_self archival handoff context for %s: %s",
            session_id,
            detail,
            exc_info=True,
        )
        return

    if not result.get("success"):
        logger.debug(
            "compact_self archival handoff refresh for %s did not succeed: %s",
            session_id,
            result.get("error") or result.get("full_error") or "unknown error",
        )


def _schedule_compact_handoff_background_refresh(
    session_id: str,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any | None,
    session_summary_config: Any | None,
) -> bool:
    coro = _run_compact_handoff_background_refresh(
        session_id,
        session_manager,
        db,
        llm_service,
        session_summary_config,
    )
    try:
        asyncio.create_task(coro, name=f"compact-handoff-refresh-{session_id[:8]}")
    except RuntimeError as exc:
        coro.close()
        logger.debug(
            "Failed scheduling compact_self archival handoff refresh for %s: %s",
            session_id,
            exc,
        )
        return False
    return True


async def _refresh_compact_handoff_context(
    session_id: str,
    session: Any,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any | None,
    session_summary_config: Any | None,
) -> dict[str, Any]:
    """Prepare summary_markdown quickly before compact_self sends /compact."""
    from gobby.mcp_proxy.tools.sessions._summary_metadata import (
        compact_summary_metadata_matches,
    )

    if await compact_summary_metadata_matches(
        session=session,
        session_manager=session_manager,
        db=db,
        session_summary_config=session_summary_config,
    ):
        return _mark_compact_handoff_ready(
            session_id,
            session,
            session_manager,
            fallback=False,
        )

    digest_markdown = getattr(session, "digest_markdown", None)
    if isinstance(digest_markdown, str) and digest_markdown.strip():
        return await _persist_compact_handoff_fallback(
            session_id,
            session,
            session_manager,
            reason="summary metadata stale or missing",
        )

    existing_summary = _valid_existing_summary_markdown(session)
    if existing_summary:
        result = _mark_compact_handoff_ready(
            session_id,
            session,
            session_manager,
            fallback=True,
        )
        if result.get("success"):
            result["background_refresh_needed"] = _has_summary_refresh_source(session)
        return result

    return await _persist_compact_handoff_fallback(
        session_id,
        session,
        session_manager,
        reason="digest missing",
    )
