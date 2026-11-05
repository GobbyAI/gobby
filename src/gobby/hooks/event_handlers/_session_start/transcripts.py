"""Transcript discovery for session-start handlers."""

from __future__ import annotations

from typing import Any

from gobby.hooks.codex_seat import codex_thread_originator
from gobby.hooks.events import HookEventType
from gobby.sessions.machine_scope import (
    is_local_machine_owner,
)
from gobby.sessions.transcript_paths import (
    MISSING_TRANSCRIPT_PATH,
    TranscriptPathStatus,
    classify_transcript_path,
    find_transcript_on_disk,
    hook_transcript_is_bound,
    usable_transcript_path,
)

MAX_PENDING_TRANSCRIPT_RECHECKS = 8
PENDING_TRANSCRIPT_RECHECK_EVENTS = frozenset(
    {
        HookEventType.BEFORE_TOOL,
        HookEventType.AFTER_TOOL,
        HookEventType.AFTER_AGENT,
        HookEventType.STOP,
    }
)


def replace_session_message_processor(
    handler: Any,
    session_id: str,
    processor: Any,
    transcript_path: str,
    *,
    source: str,
) -> None:
    """Register a processor first, then drop only a different previous entry."""
    processor.register_session(session_id, transcript_path, source=source)
    previous = handler._session_message_processors.get(session_id)
    if previous is not None and previous is not processor:
        try:
            previous.unregister_session(session_id)
        except Exception as exc:
            handler.logger.warning(
                "Failed to unregister previous session message processor: %s",
                exc,
            )
    handler._session_message_processors[session_id] = processor


def derive_transcript_path(
    handler: Any,
    cli_source: str,
    input_data: dict[str, Any],
    external_id: str,
    *,
    owner_machine_id: str | None,
    local_machine_id: str | None,
    stored_path: str | None = None,
) -> str | None:
    """Resolve a persistable transcript path: hook-first, then bounded disk fallback."""
    del handler
    if not is_local_machine_owner(owner_machine_id, local_machine_id):
        return None
    status, reported = classify_transcript_path(input_data.get("transcript_path"))
    cwd = input_data.get("cwd")
    cwd_text = str(cwd) if cwd else None
    if (
        status is TranscriptPathStatus.USABLE
        and reported is not None
        and _hook_payload_names_session(cli_source, reported, external_id, cwd_text)
    ):
        return reported
    stored = usable_transcript_path(stored_path)
    if stored is not None:
        return stored
    # The payload's sessionId is not trusted: the disk fallback keys on external_id only.
    return find_transcript_on_disk(
        cli_source,
        external_id,
        owner_machine_id=owner_machine_id,
        local_machine_id=local_machine_id,
        caller_context="hook",
        cwd=cwd_text,
    )


def _hook_payload_names_session(
    cli_source: str, reported: str, external_id: str, cwd: str | None
) -> bool:
    """Whether a usable hook-reported transcript is this session's own, by trusted state.

    A provider layout bound to ``external_id`` wins. Codex rollouts carry no path
    layout, so their session_meta header must name this thread.
    """
    if hook_transcript_is_bound(cli_source, reported, external_id, cwd=cwd):
        return True
    return cli_source == "codex" and codex_thread_originator(reported, external_id) is not None


def recheck_pending_transcript_path(
    event: Any,
    *,
    session_manager: Any,
    budgets: dict[str, int],
    local_machine_id: str | None,
) -> tuple[str, str, str] | None:
    """Resolve transcript tracking after start, including a missed SessionStart."""
    if event.event_type not in PENDING_TRANSCRIPT_RECHECK_EVENTS:
        return None
    if session_manager is None:
        return None
    platform_session_id = event.metadata.get("_platform_session_id")
    if not isinstance(platform_session_id, str) or not platform_session_id:
        return None
    session = session_manager.get(platform_session_id)
    if session is None:
        return None
    stored = getattr(session, "transcript_path", None)
    source = str(session.source)
    # A stored path that no longer reads (Claude relocates the transcript with
    # the cwd) is re-derived like a missing one, within the same bounded budget.
    if stored and stored != MISSING_TRANSCRIPT_PATH and usable_transcript_path(stored):
        return platform_session_id, stored, source
    attempts = budgets.get(platform_session_id, 0)
    if attempts >= MAX_PENDING_TRANSCRIPT_RECHECKS:
        return None
    budgets[platform_session_id] = attempts + 1
    external_id = str(event.session_id or getattr(session, "external_id", "") or "").strip()
    path = derive_transcript_path(
        None,
        source,
        event.data if isinstance(event.data, dict) else {},
        external_id,
        owner_machine_id=getattr(session, "machine_id", None),
        local_machine_id=local_machine_id,
        stored_path=stored,
    )
    if not path:
        return None
    session_manager.update(platform_session_id, transcript_path=path)
    budgets.pop(platform_session_id, None)
    return platform_session_id, path, source
