"""Qwen CLI terminal-hook adapter."""

from typing import Any

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.claude_contract import ClaudeDecisionStyle
from gobby.adapters.qwen_contract import (
    QWEN_EVENT_MAP,
    QWEN_HOOK_EVENT_NAME_MAP,
    QwenHookContract,
    get_qwen_contract,
)
from gobby.hooks.events import HookResponse, SessionSource


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
