"""Compact handoff context helpers for terminal session tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.sessions._terminal_transcripts import _read_transcript_tail_lines

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.sessions import SessionSummaryConfig
    from gobby.config.tasks import CompactHandoffConfig
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

_DEFAULT_COMPACT_HANDOFF_REFRESH_TIMEOUT_SECONDS = 300.0
_COMPACT_HANDOFF_FALLBACK_MAX_CHARS = 20_000
COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS = 3


def _has_summary_refresh_source(session: Any) -> bool:
    """Return whether summary generation has current session content to read."""
    digest_markdown = getattr(session, "digest_markdown", None)
    if isinstance(digest_markdown, str) and digest_markdown.strip():
        return True

    transcript_path = getattr(session, "transcript_path", None)
    return isinstance(transcript_path, str) and bool(transcript_path.strip())


def _capture_handoff_configs(
    config_resolver: Callable[[], DaemonConfig | None] | None,
    *,
    session_summary_config: SessionSummaryConfig | None,
    compact_handoff_config: CompactHandoffConfig | None,
) -> tuple[SessionSummaryConfig | None, CompactHandoffConfig | None]:
    """Capture one active configuration revision for a compact operation."""
    if config_resolver is None:
        return session_summary_config, compact_handoff_config
    active = config_resolver()
    if active is None:
        return session_summary_config, compact_handoff_config
    return active.session_summary, active.compact_handoff


def _compact_handoff_refresh_timeout_seconds(
    compact_handoff: CompactHandoffConfig | None = None,
) -> float:
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
    withheld_pair: dict[str, str] | None = None,
) -> str | None:
    """Build a bounded transcript-tail fallback when no digest is available."""
    if withheld_pair is not None:
        prompt = str(withheld_pair.get("prompt") or "")
        activity = str(withheld_pair.get("activity") or "")
        response = str(withheld_pair.get("response") or "")
        fallback = (
            "# Compact Handoff\n\n"
            f"Archival handoff refresh is running in the background ({reason}). "
            "This handoff preserves the compact-triggering turn.\n\n"
            "## Compact-triggering prompt\n\n"
            f"{prompt}"
        )
        if len(fallback) >= _COMPACT_HANDOFF_FALLBACK_MAX_CHARS:
            return fallback

        activity_header = "\n\n## Tool activity (in flight)\n\n"
        activity_budget = _COMPACT_HANDOFF_FALLBACK_MAX_CHARS - len(fallback) - len(activity_header)
        bounded_activity = _bounded_newest_ledger_lines(activity, activity_budget)
        if bounded_activity:
            fallback += activity_header + bounded_activity

        response_section = f"\n\n## Narration so far\n\n{response}"
        if (
            response
            and len(fallback) + len(response_section) <= _COMPACT_HANDOFF_FALLBACK_MAX_CHARS
        ):
            fallback += response_section
        return fallback

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


def _bounded_newest_ledger_lines(activity: str, budget: int) -> str:
    """Keep newest complete ledger lines within ``budget`` characters."""
    lines = activity.splitlines()
    if not lines or budget <= 0:
        return ""
    complete = "\n".join(lines).strip()
    if len(complete) <= budget:
        return complete

    selected: list[str] = []
    for line in reversed(lines):
        candidate = [line, *selected]
        omitted = len(lines) - len(candidate)
        marker = f"[{omitted} earlier ledger lines truncated]\n" if omitted else ""
        rendered = marker + "\n".join(candidate)
        if len(rendered) > budget:
            break
        selected = candidate

    if not selected:
        return ""
    omitted = len(lines) - len(selected)
    marker = f"[{omitted} earlier ledger lines truncated]\n" if omitted else ""
    return marker + "\n".join(selected)


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
    tail_withheld: bool = False,
    withheld_pair: dict[str, str] | None = None,
) -> dict[str, Any]:
    from gobby.sessions.summary_refresh import digest_turn_count

    fallback = None
    if withheld_pair is not None:
        fallback = await _compact_handoff_transcript_tail_markdown(
            session,
            reason=reason,
            withheld_pair=withheld_pair,
        )
    if fallback is None:
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
                metadata_json={
                    "reason": reason,
                    "source": "compact_self",
                    "tail_withheld": tail_withheld,
                },
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


def _digest_failure_reason(outcome: dict[str, Any] | None) -> str | None:
    if outcome is None:
        return None
    if "error" in outcome:
        return str(outcome.get("error") or "digest failed")
    if outcome.get("cancelled"):
        return str(outcome.get("reason") or "digest cancelled")
    return None


def _digest_fallback_evidence(
    outcome: dict[str, Any] | None,
    withheld_capture: dict[str, Any],
) -> tuple[dict[str, str] | None, bool]:
    pair_value = (
        withheld_capture.get("withheld_pair")
        if "withheld_pair" in withheld_capture
        else (outcome or {}).get("withheld_pair")
    )
    pair = pair_value if isinstance(pair_value, dict) else None
    tail_withheld = bool(
        withheld_capture.get("tail_withheld")
        if "tail_withheld" in withheld_capture
        else (outcome or {}).get("tail_withheld")
    )
    return pair, tail_withheld


async def _digest_pending_compact_turn(
    *,
    session_id: str,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any,
    memory_manager: Any,
    config: Any | None,
    withheld_capture: dict[str, Any],
) -> dict[str, Any] | None:
    from gobby.memory.digest import build_turn_and_digest

    outcome: dict[str, Any] | None = None
    for _attempt in range(1 + COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS):
        outcome = await build_turn_and_digest(
            memory_manager=memory_manager,
            session_manager=session_manager,
            session_id=session_id,
            llm_service=llm_service,
            db=db,
            config=config,
            withheld_capture=withheld_capture,
        )
        if outcome is None:
            return None
        if outcome.get("error_kind") == "transcript_read":
            return outcome
        if _digest_failure_reason(outcome) is not None:
            return outcome
        if not outcome.get("tail_withheld"):
            return outcome
    return outcome


async def _run_compact_handoff_background_refresh(
    session_id: str,
    session_manager: SessionManager,
    db: HubDatabase,
    llm_service: Any | None,
    session_summary_config: SessionSummaryConfig | None,
    compact_handoff_config: CompactHandoffConfig | None = None,
    *,
    memory_manager: Any | None = None,
    config: Any | None = None,
) -> None:
    from gobby.sessions.summarize import generate_session_summaries

    timeout_seconds = _compact_handoff_refresh_timeout_seconds(compact_handoff_config)

    async def _refresh() -> dict[str, Any] | None:
        if memory_manager is not None and llm_service is not None:
            outcome = await _digest_pending_compact_turn(
                session_id=session_id,
                session_manager=session_manager,
                db=db,
                llm_service=llm_service,
                memory_manager=memory_manager,
                config=config,
                withheld_capture={},
            )
            if outcome is not None and outcome.get("error_kind") == "transcript_read":
                logger.warning(
                    "compact_self archival digest hit transcript corruption for %s: %s",
                    session_id,
                    outcome,
                )
                return None
            if (failure_reason := _digest_failure_reason(outcome)) is not None:
                logger.debug(
                    "compact_self archival digest failed for %s: %s",
                    session_id,
                    failure_reason,
                )
                return None
            if outcome is not None and outcome.get("tail_withheld"):
                logger.debug(
                    "compact_self archival digest still has an in-flight tail for %s",
                    session_id,
                )
                return None
            session_manager.get(session_id)

        return await generate_session_summaries(
            session_id=session_id,
            session_manager=session_manager,
            llm_service=llm_service,
            session_summary_config=session_summary_config,
            db=db,
            set_handoff_ready=False,
        )

    try:
        result = await asyncio.wait_for(_refresh(), timeout=timeout_seconds)
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

    if result is not None and not result.get("success"):
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
    session_summary_config: SessionSummaryConfig | None,
    compact_handoff_config: CompactHandoffConfig | None = None,
    *,
    memory_manager: Any | None = None,
    config: Any | None = None,
) -> bool:
    coro = _run_compact_handoff_background_refresh(
        session_id,
        session_manager,
        db,
        llm_service,
        session_summary_config,
        compact_handoff_config,
        memory_manager=memory_manager,
        config=config,
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
    session_summary_config: SessionSummaryConfig | None,
    *,
    memory_manager: Any | None = None,
    config: Any | None = None,
    compact_handoff_config: CompactHandoffConfig | None = None,
) -> dict[str, Any]:
    """Prepare summary_markdown quickly before compact_self sends /compact."""
    from gobby.mcp_proxy.tools.sessions._summary_metadata import (
        compact_summary_metadata_matches,
    )

    if memory_manager is not None and llm_service is not None:
        withheld_capture: dict[str, Any] = {}
        timeout_seconds = _compact_handoff_refresh_timeout_seconds(compact_handoff_config)
        outcome: dict[str, Any] | None = None
        try:
            outcome = await asyncio.wait_for(
                _digest_pending_compact_turn(
                    session_id=session_id,
                    session_manager=session_manager,
                    db=db,
                    llm_service=llm_service,
                    memory_manager=memory_manager,
                    config=config,
                    withheld_capture=withheld_capture,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            session = session_manager.get(session_id) or session
            withheld_pair, tail_withheld = _digest_fallback_evidence(
                outcome,
                withheld_capture,
            )
            result = await _persist_compact_handoff_fallback(
                session_id,
                session,
                session_manager,
                reason=f"pre-summary digest timed out after {timeout_seconds:g}s",
                tail_withheld=tail_withheld,
                withheld_pair=withheld_pair,
            )
            result["timed_out"] = True
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session = session_manager.get(session_id) or session
            withheld_pair, tail_withheld = _digest_fallback_evidence(
                outcome,
                withheld_capture,
            )
            detail = str(exc) or type(exc).__name__
            return await _persist_compact_handoff_fallback(
                session_id,
                session,
                session_manager,
                reason=detail,
                tail_withheld=tail_withheld,
                withheld_pair=withheld_pair,
            )

        if outcome is not None and outcome.get("error_kind") == "transcript_read":
            detail = str(outcome.get("error") or "transcript corruption")
            existing_summary = _valid_existing_summary_markdown(session)
            if not existing_summary:
                return {
                    "success": False,
                    "error": detail,
                    "digest_failure_reason": detail,
                }
            result = _mark_compact_handoff_ready(
                session_id,
                session,
                session_manager,
                fallback=True,
            )
            result["digest_failure_reason"] = detail
            return result

        if (failure_reason := _digest_failure_reason(outcome)) is not None:
            session = session_manager.get(session_id) or session
            withheld_pair, tail_withheld = _digest_fallback_evidence(
                outcome,
                withheld_capture,
            )
            result = await _persist_compact_handoff_fallback(
                session_id,
                session,
                session_manager,
                reason=failure_reason,
                tail_withheld=tail_withheld,
                withheld_pair=withheld_pair,
            )
            result["digest_failure_reason"] = failure_reason
            return result

        if outcome is not None and outcome.get("tail_withheld"):
            session = session_manager.get(session_id) or session
            withheld_pair, tail_withheld = _digest_fallback_evidence(
                outcome,
                withheld_capture,
            )
            return await _persist_compact_handoff_fallback(
                session_id,
                session,
                session_manager,
                reason="transcript tail in-flight",
                tail_withheld=tail_withheld,
                withheld_pair=withheld_pair,
            )

        session = session_manager.get(session_id) or session

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
