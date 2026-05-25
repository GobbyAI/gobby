"""Plan-mode detection observer for workflow session variables."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("gobby.workflows.observers")

_MODE_LEVEL_MAP = {"plan": 0, "accept_edits": 1, "normal": 1, "bypass": 2}


def compute_mode_level(chat_mode: str) -> int:
    """Derive numeric mode_level from chat_mode.

    Returns 0 (Plan), 1 (Act), or 2 (YOLO).
    """
    return _MODE_LEVEL_MAP.get(chat_mode, 2)


def _first_marker(text: str, markers: list[str] | tuple[str, ...]) -> str | None:
    """Return the first configured marker present in text."""
    return next((marker for marker in markers if marker in text), None)


def detect_plan_mode_from_context(
    prompt: str | None, variables: dict[str, Any], session_id: str
) -> None:
    """Detect plan mode from system reminders or CLI-specific markers."""
    if not prompt:
        return

    cleaned = re.sub(
        r"<conversation-history>.*?</conversation-history>", "", prompt, flags=re.DOTALL
    )

    system_reminders = re.findall(r"<system-reminder>(.*?)</system-reminder>", cleaned, re.DOTALL)
    reminder_text = " ".join(system_reminders)

    def set_mode(chat_mode: str, reason: str) -> None:
        variables["chat_mode"] = chat_mode
        level = compute_mode_level(chat_mode)
        if variables.get("mode_level") != level:
            variables["mode_level"] = level
            logger.info("Session %s: mode_level=%s (%s)", session_id, level, reason)
        if level != 0 and (variables.get("plan_mode") or variables.get("plan_skill_loaded")):
            variables["plan_mode"] = False
            variables["plan_skill_loaded"] = False
            logger.info("Session %s: plan_mode=False", session_id)

    plan_mode_indicators = [
        "Plan mode is active",
        "Plan mode still active",
        "You are in plan mode",
    ]

    indicator = _first_marker(reminder_text, plan_mode_indicators)
    if indicator:
        if variables.get("mode_level") != 0:
            variables["mode_level"] = 0
            logger.info(
                "Session %s: mode_level=0 (plan) (detected from system reminder: %r)",
                session_id,
                indicator,
            )
        if not variables.get("plan_mode"):
            variables["plan_mode"] = True
            logger.info("Session %s: plan_mode=True", session_id)
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
        if variables.get("mode_level") == 0:
            chat_mode = variables.get("chat_mode", "bypass")
            variables["mode_level"] = compute_mode_level(chat_mode)
            logger.info(
                "Session %s: mode_level=%s (detected from system reminder: %r)",
                session_id,
                variables["mode_level"],
                indicator,
            )
        if variables.get("plan_mode"):
            variables["plan_mode"] = False
            variables["plan_skill_loaded"] = False
            logger.info("Session %s: plan_mode=False", session_id)
        return

    gemini_plan_indicators = [
        "# Active Approval Mode: Plan",
        "You are operating in **Plan Mode**",
    ]

    indicator = _first_marker(cleaned, gemini_plan_indicators)
    if indicator:
        if variables.get("mode_level") != 0:
            variables["mode_level"] = 0
            logger.info(
                "Session %s: mode_level=0 (plan) (detected from Gemini marker: %r)",
                session_id,
                indicator,
            )
        return

    gemini_exit_indicators = [
        "Exited Plan Mode",
        "# Active Approval Mode: Execute",
    ]

    indicator = _first_marker(cleaned, gemini_exit_indicators)
    if indicator:
        if variables.get("mode_level") == 0:
            chat_mode = variables.get("chat_mode", "bypass")
            variables["mode_level"] = compute_mode_level(chat_mode)
            logger.info(
                "Session %s: mode_level=%s (detected from Gemini marker: %r)",
                session_id,
                variables["mode_level"],
                indicator,
            )
        return

    if '<plan-mode status="active">' in cleaned:
        if variables.get("mode_level") != 0:
            variables["mode_level"] = 0
            logger.info(
                'Session %s: mode_level=0 (plan) (detected from <plan-mode status="active">)',
                session_id,
            )
        return

    if '<plan-mode status="approved">' in cleaned:
        if variables.get("mode_level") == 0:
            chat_mode = variables.get("chat_mode", "bypass")
            variables["mode_level"] = compute_mode_level(chat_mode)
            logger.info(
                'Session %s: mode_level=%s (detected from <plan-mode status="approved">)',
                session_id,
                variables["mode_level"],
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
        chat_mode = variables.get("chat_mode", "bypass")
        new_level = compute_mode_level(chat_mode)
        if new_level != 0:
            variables["mode_level"] = new_level
            logger.info(
                "Session %s: mode_level=%s "
                "(healed stale plan mode - no markers found, chat_mode=%r)",
                session_id,
                new_level,
                chat_mode,
            )
