"""Transcript discovery for session-start handlers."""

from __future__ import annotations

import hashlib
from typing import Any, cast
from urllib.parse import quote

from gobby.sessions.machine_scope import (
    RemoteSessionOwnershipError,
    is_local_machine_owner,
    require_local_session_ownership,
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
) -> str | None:
    """Derive transcript path for CLIs that do not provide one natively."""
    if not is_local_machine_owner(owner_machine_id, local_machine_id):
        return None
    if cli_source == "qwen":
        return cast(str | None, handler._find_qwen_transcript(input_data, external_id))
    if cli_source == "grok":
        return find_grok_transcript(handler, input_data, external_id)
    return None


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


def find_grok_transcript(
    handler: Any,
    input_data: dict[str, Any],
    external_id: str,
) -> str | None:
    """Derive Grok ``updates.jsonl`` path for the hook event."""
    cwd = input_data.get("cwd")
    if not cwd:
        handler.logger.debug("Cannot derive Grok transcript: no cwd")
        return None

    session_id = input_data.get("sessionId") or input_data.get("session_id") or external_id
    if not session_id:
        handler.logger.debug("Cannot derive Grok transcript: no session id")
        return None

    encoded_cwd = quote(str(cwd), safe="")
    path = (
        _compat_module().Path.home()
        / ".grok"
        / "sessions"
        / encoded_cwd
        / str(session_id)
        / "updates.jsonl"
    )
    return str(path)


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
