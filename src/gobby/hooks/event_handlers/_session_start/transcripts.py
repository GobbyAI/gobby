"""Transcript discovery for session-start handlers."""

from __future__ import annotations

import hashlib
from typing import Any, cast
from urllib.parse import quote


def _compat_module() -> Any:
    import gobby.hooks.event_handlers._session_start as session_start

    return session_start


def derive_transcript_path(
    handler: Any,
    cli_source: str,
    input_data: dict[str, Any],
    external_id: str,
) -> str | None:
    """Derive transcript path for CLIs that do not provide one natively."""
    if cli_source == "gemini":
        return cast(str | None, handler._find_gemini_transcript(input_data, external_id))
    if cli_source == "qwen":
        return cast(str | None, handler._find_qwen_transcript(input_data, external_id))
    if cli_source == "grok":
        return find_grok_transcript(handler, input_data, external_id)
    return None


def find_gemini_transcript(
    handler: Any,
    input_data: dict[str, Any],
    external_id: str,
) -> str | None:
    """Locate a Gemini CLI JSON session transcript for the hook event."""
    return cast(
        str | None,
        handler._find_json_session_transcript("gemini", "Gemini", input_data, external_id),
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

    all_sessions = sorted(chats_dir.glob("session-*.json"), reverse=True)
    if all_sessions:
        handler.logger.debug("Found %s transcript (most recent): %s", cli_label, all_sessions[0])
        return str(all_sessions[0])

    handler.logger.debug("No %s session files in %s", cli_label, chats_dir)
    return None
