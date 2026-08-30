"""Transcript discovery for session-start handlers."""

from __future__ import annotations

import hashlib
from typing import Any, cast

from gobby.hooks.events import HookEventType
from gobby.sessions.machine_scope import (
    RemoteSessionOwnershipError,
    is_local_machine_owner,
    require_local_session_ownership,
)
from gobby.sessions.transcript_paths import (
    MISSING_TRANSCRIPT_PATH,
    TranscriptPathStatus,
    classify_transcript_path,
    find_transcript_on_disk,
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


def _compat_module() -> Any:
    import gobby.hooks.event_handlers._session_start as session_start

    return session_start


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
    if status is TranscriptPathStatus.USABLE:
        return reported
    stored = usable_transcript_path(stored_path)
    if stored is not None:
        return stored
    session_id = input_data.get("sessionId") or input_data.get("session_id") or external_id
    cwd = input_data.get("cwd")
    return find_transcript_on_disk(
        cli_source,
        external_id,
        owner_machine_id=owner_machine_id,
        local_machine_id=local_machine_id,
        caller_context="hook",
        cwd=str(cwd) if cwd else None,
        session_id=str(session_id) if session_id else None,
    )


def find_qwen_transcript(
    handler: Any,
    input_data: dict[str, Any],
    external_id: str,
) -> str | None:
    """Locate a Qwen CLI JSON session transcript for the hook event."""
    return cast(
        str | None,
        handler._find_json_session_transcript("qwen", "Qwen", input_data, external_id),
    )


def ensure_qwen_transcript_tracking(
    handler: Any,
    event: Any,
    platform_session_id: str,
) -> str | None:
    """Derive and register a Qwen transcript on the first hook after creation."""
    if event.source.value != "qwen":
        return None

    session = (
        handler._session_manager.get(platform_session_id) if handler._session_manager else None
    )
    if session is None:
        return None
    try:
        require_local_session_ownership(session)
    except RemoteSessionOwnershipError:
        return None
    transcript_path = getattr(session, "transcript_path", None)
    if not isinstance(transcript_path, str) or not transcript_path:
        transcript_path = find_qwen_transcript(
            handler,
            event.data,
            str(event.session_id or "").strip(),
        )
        if not transcript_path:
            return None
        if handler._session_manager:
            handler._session_manager.update(
                platform_session_id,
                transcript_path=transcript_path,
            )
        event.data["transcript_path"] = transcript_path

    if handler._session_coordinator:
        handler._session_coordinator.register_session(
            str(event.session_id or "").strip() or platform_session_id
        )
    message_processor = handler._resolve_message_processor()
    if message_processor is not None:
        replace_session_message_processor(
            handler,
            platform_session_id,
            message_processor,
            transcript_path,
            source="qwen",
        )
    return transcript_path


def find_json_session_transcript(
    handler: Any,
    cli_name: str,
    cli_label: str,
    input_data: dict[str, Any],
    external_id: str,
) -> str | None:
    """Find a JSON session transcript for supported CLIs."""
    cwd = input_data.get("cwd")
    if not cwd:
        handler.logger.debug("Cannot derive %s transcript: no cwd", cli_label)
        return None

    session_id = input_data.get("session_id") or external_id or ""
    project_hash = hashlib.sha256(cwd.encode()).hexdigest()
    chats_dir = _compat_module().Path.home() / f".{cli_name}" / "tmp" / project_hash / "chats"

    if not chats_dir.exists():
        handler.logger.debug("%s chats dir not found: %s", cli_label, chats_dir)
        return None

    prefix = session_id[:8] if session_id else ""
    if prefix:
        matches = sorted(chats_dir.glob(f"session-*-{prefix}.json"), reverse=True)
        if matches:
            handler.logger.debug("Found %s transcript by prefix: %s", cli_label, matches[0])
            return str(matches[0])

    handler.logger.debug(
        "No %s session file matching prefix %s in %s",
        cli_label,
        prefix or "<missing>",
        chats_dir,
    )
    return None


def recheck_pending_transcript_path(
    event: Any,
    *,
    session_manager: Any,
    budgets: dict[str, int],
    local_machine_id: str | None,
) -> None:
    """Persist a usable transcript path on later hook events without blocking start."""
    if event.event_type not in PENDING_TRANSCRIPT_RECHECK_EVENTS:
        return
    if session_manager is None:
        return
    platform_session_id = event.metadata.get("_platform_session_id")
    if not isinstance(platform_session_id, str) or not platform_session_id:
        return
    session = session_manager.get(platform_session_id)
    if session is None:
        return
    stored = getattr(session, "transcript_path", None)
    if stored and stored != MISSING_TRANSCRIPT_PATH:
        return
    attempts = budgets.get(platform_session_id, 0)
    if attempts >= MAX_PENDING_TRANSCRIPT_RECHECKS:
        return
    budgets[platform_session_id] = attempts + 1
    source = event.source.value if hasattr(event.source, "value") else str(event.source)
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
        return
    session_manager.update(platform_session_id, transcript_path=path)
    budgets.pop(platform_session_id, None)
