"""Plan-mode detection observer for workflow session variables."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from gobby.hooks.events import HookEvent, SessionSource
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_requirements import append_request_anchor, capture_request_anchor

logger = logging.getLogger("gobby.workflows.observers")


class _SessionValue(Protocol):
    session_type: object
    chat_mode: object
    transcript_path: object


class _SessionManager(Protocol):
    def get(self, session_id: str) -> _SessionValue | None: ...


_MODE_ALIASES = {
    "plan": "plan",
    "planning": "plan",
    "normal": "normal",
    "act": "normal",
    "acceptedits": "normal",
    "default": "normal",
    "execute": "normal",
    "bypass": "bypass",
    "bypasspermissions": "bypass",
    "auto": "bypass",
    "fullauto": "bypass",
    "yolo": "bypass",
}

_MODE_LEVEL_MAP = {"plan": 0, "accept_edits": 1, "normal": 1, "bypass": 2}


def compute_mode_level(chat_mode: str) -> int:
    """Derive numeric mode_level from chat_mode.

    Returns 0 (Plan), 1 (Act), or 2 (YOLO).
    """
    return _MODE_LEVEL_MAP.get(chat_mode, 2)


def _first_marker(text: str, markers: list[str] | tuple[str, ...]) -> str | None:
    """Return the first configured marker present in text."""
    return next((marker for marker in markers if marker in text), None)


def resolve_plan_mode(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
) -> None:
    """Resolve the current mode from the authoritative source for this surface."""
    session = _load_session(session_manager, session_id)
    metadata = event.metadata or {}
    data = event.data or {}
    session_type = metadata.get("session_type") or getattr(session, "session_type", None)
    prompt = data.get("prompt")
    request_content = prompt if isinstance(prompt, str) and prompt else None
    request_anchor_id = _request_anchor_id(data, session_id, request_content)

    if session_type == "web_chat":
        mode = _normalize_mode(metadata.get("chat_mode"))
        reason = "managed web-chat runtime metadata"
        if mode is None:
            mode = _normalize_mode(getattr(session, "chat_mode", None))
            reason = "persisted web-chat session"
        if mode is not None:
            _apply_resolved_mode(
                variables,
                session_id,
                mode,
                reason,
                persist_plan_mode=True,
                request_anchor_id=request_anchor_id,
                request_content=request_content,
            )
        return

    structured_mode = _normalize_mode(metadata.get("chat_mode")) or _normalize_mode(
        data.get("chat_mode")
    )
    if structured_mode is not None:
        _apply_resolved_mode(
            variables,
            session_id,
            structured_mode,
            "structured hook mode",
            persist_plan_mode=False,
            request_anchor_id=request_anchor_id,
            request_content=request_content,
        )
        return

    if event.source is SessionSource.CODEX:
        codex_mode = _latest_codex_collaboration_mode(getattr(session, "transcript_path", None))
        if codex_mode is not None:
            _apply_resolved_mode(
                variables,
                session_id,
                codex_mode,
                "Codex turn_context collaboration mode",
                persist_plan_mode=False,
                request_anchor_id=request_anchor_id,
                request_content=request_content,
            )
            return

    native_mode = _provider_native_mode(data)
    if native_mode is not None:
        _apply_resolved_mode(
            variables,
            session_id,
            native_mode,
            "provider-native hook state",
            persist_plan_mode=False,
            request_anchor_id=request_anchor_id,
            request_content=request_content,
        )
        return

    workflow_mode = _normalize_mode(variables.get("chat_mode"))
    if workflow_mode is None and (variables.get("mode_level") == 0 or variables.get("plan_mode")):
        workflow_mode = "plan"
    if workflow_mode is not None:
        _apply_resolved_mode(
            variables,
            session_id,
            workflow_mode,
            "workflow variables",
            persist_plan_mode=True,
            request_anchor_id=request_anchor_id,
            request_content=request_content,
        )

    detect_plan_mode_from_context(
        prompt if isinstance(prompt, str) else None,
        variables,
        session_id,
        request_anchor_id=request_anchor_id,
    )


def reconcile_native_mode(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
) -> None:
    """Apply the provider-stamped permission mode carried by this event.

    Claude Code stamps ``permission_mode`` on tool and stop payloads but omits
    it on UserPromptSubmit, and a manual plan-mode toggle fires no hook, so
    turn-start resolution can hold ``plan_mode`` stale indefinitely after an
    unapproved plan-mode exit. Tool-event reconciliation runs before rule
    evaluation, so the first gated edit after such an exit clears the stale
    flag instead of being blocked. Codex permission values describe sandbox
    state, so its collaboration mode stays owned by transcript turn-context
    resolution. Web-chat sessions and structured ``chat_mode`` signals stay
    owned by turn-start resolution.
    """
    metadata = event.metadata or {}
    data = event.data or {}
    if metadata.get("session_type") == "web_chat":
        return
    if event.source is SessionSource.CODEX:
        return
    if _normalize_mode(metadata.get("chat_mode")) is not None:
        return
    if _normalize_mode(data.get("chat_mode")) is not None:
        return
    mode = _provider_native_mode(data)
    if mode is None:
        return
    try:
        _apply_resolved_mode(
            variables,
            session_id,
            mode,
            "provider-native tool-event state",
            persist_plan_mode=False,
            request_anchor_id=_request_anchor_id(data, session_id, None),
            request_content=None,
        )
    except ReviewEvidenceError:
        logger.debug(
            "Session %s: native mode %r not applied - plan entry lacks a request anchor",
            session_id,
            mode,
        )


def _load_session(session_manager: _SessionManager | None, session_id: str) -> _SessionValue | None:
    if session_manager is None:
        return None
    try:
        return session_manager.get(session_id)
    except Exception:
        logger.debug(
            "Failed to load session %s while resolving plan mode", session_id, exc_info=True
        )
        return None


def _normalize_mode(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s_-]+", "", value).lower()
    return _MODE_ALIASES.get(key)


def _request_anchor_id(
    data: dict[str, Any],
    session_id: str,
    content: str | None,
) -> str:
    for key in ("request_id", "message_id", "turn_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    if content is None:
        return f"{session_id}:persisted"
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    return f"{session_id}:{digest}"


def _provider_native_mode(data: dict[str, Any]) -> str | None:
    for key in (
        "permission_mode",
        "permissionMode",
        "approval_mode",
        "approvalMode",
        "current_mode",
        "currentMode",
        "mode",
    ):
        mode = _normalize_mode(data.get(key))
        if mode is not None:
            return mode
    return None


def _apply_resolved_mode(
    variables: dict[str, Any],
    session_id: str,
    mode: str,
    reason: str,
    *,
    persist_plan_mode: bool,
    request_anchor_id: str,
    request_content: str | None,
) -> None:
    level = compute_mode_level(mode)
    persist_mode = mode != "plan" or persist_plan_mode
    mode_changed = persist_mode and variables.get("chat_mode") != mode
    level_changed = variables.get("mode_level") != level
    is_plan = level == 0
    plan_changed = bool(variables.get("plan_mode")) != is_plan

    if plan_changed:
        _set_plan_mode_with_anchor(
            variables,
            is_plan=is_plan,
            request_anchor_id=request_anchor_id,
            request_content=request_content,
        )
    elif is_plan and request_content is not None:
        append_request_anchor(variables, content=request_content)
    if persist_mode:
        variables["chat_mode"] = mode
    if level_changed:
        variables["mode_level"] = level
    if not is_plan and variables.get("plan_skill_loaded"):
        variables["plan_skill_loaded"] = False
    if mode_changed or level_changed or plan_changed:
        logger.debug(
            "Session %s: effective mode changed (mode=%s, level=%s, plan_mode=%s, reason=%s)",
            session_id,
            mode,
            level,
            is_plan,
            reason,
            extra={
                "mode": mode,
                "mode_level": level,
                "plan_mode": is_plan,
                "resolution_reason": reason,
            },
        )


def _set_plan_mode_with_anchor(
    variables: dict[str, Any],
    *,
    is_plan: bool,
    request_anchor_id: str,
    request_content: str | None,
) -> None:
    if is_plan and not bool(variables.get("plan_mode")):
        capture_request_anchor(
            variables,
            anchor_id=request_anchor_id,
            content=request_content,
        )
    variables["plan_mode"] = is_plan


def _latest_codex_collaboration_mode(transcript_path: object) -> str | None:
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        for raw_line in _reverse_jsonl_lines(Path(transcript_path)):
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            payload = record.get("payload")
            collaboration_mode = (
                payload.get("collaboration_mode") if isinstance(payload, dict) else None
            )
            mode = collaboration_mode.get("mode") if isinstance(collaboration_mode, dict) else None
            normalized = _normalize_mode(mode)
            if normalized is not None:
                return normalized
    except OSError:
        logger.debug("Unable to read Codex transcript %s", transcript_path, exc_info=True)
    return None


def _reverse_jsonl_lines(path: Path, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield non-empty JSONL records newest-first without loading the file."""
    with path.open("rb") as transcript:
        transcript.seek(0, 2)
        position = transcript.tell()
        pending = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            transcript.seek(position)
            pending = transcript.read(read_size) + pending
            lines = pending.split(b"\n")
            pending = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if pending.strip():
            yield pending


def detect_plan_mode_from_context(
    prompt: str | None,
    variables: dict[str, Any],
    session_id: str,
    *,
    request_anchor_id: str | None = None,
) -> None:
    """Detect plan mode from system reminders or CLI-specific markers."""
    if not prompt:
        return

    cleaned = re.sub(
        r"<conversation-history>.*?</conversation-history>", "", prompt, flags=re.DOTALL
    )
    anchor_id = request_anchor_id or _request_anchor_id({}, session_id, prompt)

    system_reminders = re.findall(r"<system-reminder>(.*?)</system-reminder>", cleaned, re.DOTALL)
    reminder_text = " ".join(system_reminders)

    def set_mode(
        chat_mode: str,
        reason: str,
        *,
        persist_plan_mode: bool = True,
    ) -> None:
        _apply_resolved_mode(
            variables,
            session_id,
            chat_mode,
            reason,
            persist_plan_mode=persist_plan_mode,
            request_anchor_id=anchor_id,
            request_content=prompt,
        )

    plan_mode_indicators = [
        "Plan mode is active",
        "Plan mode still active",
        "You are in plan mode",
    ]

    indicator = _first_marker(reminder_text, plan_mode_indicators)
    if indicator:
        set_mode(
            "plan",
            f"detected from system reminder: {indicator!r}",
            persist_plan_mode=False,
        )
        return

    reminder_lower = reminder_text.lower()
    mode_indicators = [
        (
            "bypass",
            [
                "auto mode is active",
                "you are in auto mode",
                "yolo mode is active",
                "you are in yolo mode",
                "bypasspermissions",
                "permission mode is bypasspermissions",
            ],
        ),
        (
            "normal",
            [
                "act mode is active",
                "you are in act mode",
                "normal execution mode",
                "acceptedits",
                "permission mode is default",
            ],
        ),
    ]

    for chat_mode, indicators in mode_indicators:
        indicator = _first_marker(reminder_lower, indicators)
        if indicator:
            set_mode(chat_mode, f"detected from system reminder: {indicator!r}")
            return

    exit_indicators = [
        "Exited Plan Mode",
        "Plan mode exited",
    ]

    indicator = _first_marker(reminder_text, exit_indicators)
    if indicator:
        chat_mode = _normalize_mode(variables.get("chat_mode")) or "bypass"
        set_mode(chat_mode, f"detected from system reminder: {indicator!r}")
        return

    acp_plan_indicators = [
        "# Active Approval Mode: Plan",
        "You are operating in **Plan Mode**",
    ]

    indicator = _first_marker(cleaned, acp_plan_indicators)
    if indicator:
        set_mode(
            "plan",
            f"detected from ACP marker: {indicator!r}",
            persist_plan_mode=False,
        )
        return

    acp_exit_indicators = [
        "Exited Plan Mode",
        "# Active Approval Mode: Execute",
    ]

    indicator = _first_marker(cleaned, acp_exit_indicators)
    if indicator:
        chat_mode = _normalize_mode(variables.get("chat_mode")) or "bypass"
        set_mode(chat_mode, f"detected from ACP marker: {indicator!r}")
        return

    if '<plan-mode status="active">' in cleaned:
        set_mode(
            "plan",
            'detected from <plan-mode status="active">',
            persist_plan_mode=False,
        )
        return

    if '<plan-mode status="approved">' in cleaned:
        chat_mode = _normalize_mode(variables.get("chat_mode")) or "bypass"
        set_mode(chat_mode, 'detected from <plan-mode status="approved">')
        return

    if re.search(r"<plan-mode(?:\s[^>]*)?>", cleaned, re.IGNORECASE):
        set_mode(
            "plan",
            "detected from <plan-mode>",
            persist_plan_mode=False,
        )
        return

    if '<chat-mode status="yolo">' in cleaned:
        set_mode("bypass", 'detected from <chat-mode status="yolo">')
        return

    if '<chat-mode status="auto">' in cleaned:
        set_mode("bypass", 'detected from legacy <chat-mode status="auto">')
        return

    if '<chat-mode status="act">' in cleaned:
        set_mode("normal", 'detected from <chat-mode status="act">')
        return

    if variables.get("mode_level") == 0:
        chat_mode = _normalize_mode(variables.get("chat_mode")) or "bypass"
        new_level = compute_mode_level(chat_mode)
        if new_level != 0:
            set_mode(
                chat_mode,
                f"healed stale plan mode - no markers found, chat_mode={chat_mode!r}",
            )
