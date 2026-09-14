"""Qwen CLI terminal-hook adapter."""

import re
from typing import Any

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.claude_contract import ClaudeDecisionStyle
from gobby.adapters.qwen_contract import (
    QWEN_EVENT_MAP,
    QWEN_HOOK_EVENT_NAME_MAP,
    QwenHookContract,
    get_qwen_contract,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource


def _normalize_router_help(prompt: str) -> str:
    """Recover help from Qwen's managed-skill expansion without reading args files."""
    header, _, body = prompt.partition("\n")
    if not header.startswith("Base directory for this skill: ") or not header.endswith(
        "/skills/gobby"
    ):
        return prompt
    if not body.startswith(
        "Important: ALWAYS resolve absolute paths from this base directory when working with skills."
        "\n\n# Gobby Router\n"
    ):
        return prompt
    _, marker, suffix = body.partition("\n<!-- gobby-router:end -->")
    if not marker:
        return prompt
    suffix = suffix.strip()
    if not suffix or re.fullmatch(r"<skill-args-stale>[^<]*</skill-args-stale>", suffix):
        return "/gobby"
    command, note_separator, note = suffix.partition(
        "\n\nYour invocation arguments have been written verbatim to a session-private file."
    )
    if command.strip() != "/gobby help":
        return prompt
    if not note_separator or re.fullmatch(
        r"[^\n]*\n<skill-args-file>[^<]+</skill-args-file>\n<skill-args>help</skill-args>",
        note,
    ):
        return "/gobby help"
    return prompt


class QwenAdapter(ClaudeCodeAdapter):
    """Translate Qwen's Claude-shaped terminal-hook protocol."""

    EVENT_MAP = dict(QWEN_EVENT_MAP)
    HOOK_EVENT_NAME_MAP = dict(QWEN_HOOK_EVENT_NAME_MAP)

    @property
    def source(self) -> SessionSource:
        return SessionSource.QWEN

    @classmethod
    def _get_hook_contract(cls, hook_type: str | None) -> QwenHookContract | None:
        return get_qwen_contract(hook_type)

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Classify Qwen's exact boolean whole-turn interruption evidence."""
        event = super().translate_to_hook_event(native_event)
        if event.event_type is HookEventType.BEFORE_AGENT:
            prompt = event.data.get("prompt")
            if isinstance(prompt, str):
                event.data["prompt"] = _normalize_router_help(prompt)
        hook_type = native_event.get("hook_type", "")
        contract = self._get_hook_contract(hook_type)
        hook_event_name = contract.hook_event_name if contract is not None else hook_type
        if hook_event_name == "PostToolUseFailure" and event.data.get("is_interrupt") is True:
            event.event_type = HookEventType.INTERRUPT
            event.turn_disposition = "user_interrupted"
        return event

    def translate_from_hook_response(
        self,
        response: HookResponse,
        hook_type: str | None = None,
    ) -> dict[str, Any]:
        result = super().translate_from_hook_response(response, hook_type=hook_type)
        contract = get_qwen_contract(hook_type)
        if contract and contract.decision_style is ClaudeDecisionStyle.TOP_LEVEL_BLOCK:
            denied = response.decision in {"deny", "block"}
            result["decision"] = "block" if denied else response.decision
            if response.reason and not denied:
                result["reason"] = response.reason
        return result
