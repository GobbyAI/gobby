"""Hook failure, timeout, and denial response shaping for the hooks route."""

import logging
from typing import Any, cast

from gobby.adapters.capabilities import ContextChannel, get_provider_capabilities
from gobby.adapters.claude_contract import get_claude_contract
from gobby.adapters.degradation import AdapterDegradationKind, record_adapter_degradation

logger = logging.getLogger(__name__)


HOLD_OPEN_HOOK_TYPE_MAP: dict[str, str] = {
    "PreToolUse": "PreToolUse",
    "pre-tool-use": "PreToolUse",
    "BeforeTool": "PreToolUse",
    "AskUserQuestion": "AskUserQuestion",
}


FAIL_SAFE_HOOK_TYPES = frozenset(hook_type.casefold() for hook_type in {"Stop", "stop"})


def _graceful_error_response(
    hook_type: str,
    error_msg: str,
    *,
    source: str | None = "claude",
) -> dict[str, Any]:
    """
    Create a graceful degradation response for hook errors.

    Instead of returning HTTP 500 (which causes Claude Code to show a confusing
    "hook failed" warning), return a successful response that:
    1. Allows the tool to proceed (continue=True)
    2. Explains the error via additionalContext (so agents understand what happened)

    This prevents agents from being confused by non-fatal hook errors.
    """
    provider = source or "claude"
    message = f"Gobby hook error (non-fatal): {error_msg}. Tool execution will proceed normally."
    record_adapter_degradation(
        provider=provider,
        hook_type=hook_type,
        kind=AdapterDegradationKind.GRACEFUL_ERROR,
        response_field="context",
        destination_channel="provider_capability",
    )

    try:
        capabilities = get_provider_capabilities(provider)
        context_channel = capabilities.context_channel_for(hook_type)
    except ValueError:
        provider = "claude"
        context_channel = get_provider_capabilities(provider).context_channel_for(hook_type)

    from gobby.hooks.events import HookResponse

    if context_channel is not ContextChannel.NONE:
        hook_response = HookResponse(decision="allow", context=message)
    else:
        hook_response = HookResponse(decision="allow", system_message=message)

    if provider == "droid":
        from gobby.adapters.droid import DroidAdapter

        result = DroidAdapter().translate_from_hook_response(hook_response, hook_type=hook_type)
        if isinstance(result, dict):
            return result

    if provider == "agy":
        from gobby.adapters.agy import AgyAdapter

        agy_response = AgyAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(agy_response, dict):
            return agy_response

    if provider == "codex":
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        codex_response = CodexHooksAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(codex_response, dict):
            return codex_response

    if provider == "grok":
        from gobby.adapters.grok import GrokAdapter

        grok_response = GrokAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(grok_response, dict):
            return grok_response

    from gobby.adapters.claude_code import ClaudeCodeAdapter

    claude_response = ClaudeCodeAdapter().translate_from_hook_response(
        hook_response,
        hook_type=hook_type,
    )
    if isinstance(claude_response, dict):
        return claude_response

    fallback: dict[str, Any] = {"continue": True}
    claude_contract = get_claude_contract(hook_type)
    if claude_contract and claude_contract.allows_additional_context:
        fallback["hookSpecificOutput"] = {
            "hookEventName": claude_contract.hook_event_name,
            "additionalContext": message,
        }
    return fallback


def _is_fail_safe_hook(hook_type: str | None, metadata: dict[str, Any]) -> bool:
    """Return whether hook failures must block for safety."""
    normalized_hook_type = hook_type.casefold() if hook_type is not None else None
    return normalized_hook_type in FAIL_SAFE_HOOK_TYPES or metadata.get("critical") is True


def _hook_block_response(
    adapter: Any | None,
    hook_type: str,
    source: str | None,
    reason: str,
) -> dict[str, Any]:
    """Translate a fail-safe block, falling back to the shared route shape."""
    from gobby.hooks.events import HookResponse

    response = HookResponse(decision="block", reason=reason)
    if adapter is None:
        return {"continue": False, "decision": "block", "reason": reason}

    try:
        translated = adapter.translate_from_hook_response(response, hook_type=hook_type)
    except TypeError:
        translated = adapter.translate_from_hook_response(response)
    except Exception:
        logger.warning(
            "Failed to translate hook block response for %s/%s",
            source,
            hook_type,
            exc_info=True,
        )
        translated = {"continue": False, "decision": "block", "reason": reason}

    return cast(dict[str, Any], translated)


def _hook_timeout_response(
    adapter: Any,
    hook_type: str,
    source: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Build a provider-native timeout response without waiting on hook internals."""
    reason = (
        f"Gobby hook evaluation timed out after {timeout_seconds:g}s; "
        "blocking this critical hook for safety. Try again after the daemon recovers."
    )
    return _hook_block_response(adapter, hook_type, source, reason)


def _hook_exception_response(
    adapter: Any | None,
    hook_type: str,
    source: str | None,
    metadata: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    """Fail closed for safety-critical hooks and degrade all other hook errors."""
    if not _is_fail_safe_hook(hook_type, metadata):
        return _graceful_error_response(hook_type, error, source=source)

    reason = (
        f"Gobby hook evaluation failed: {error}; blocking this critical hook for safety. "
        "Try again after the daemon recovers."
    )
    return _hook_block_response(adapter, hook_type, source, reason)


def _normalize_hold_open_hook_type(hook_type: str | None) -> str | None:
    """Normalize provider-specific hook names for web-chat hold-open gating."""
    if not hook_type:
        return None
    return HOLD_OPEN_HOOK_TYPE_MAP.get(hook_type)


def _result_encodes_denial(result: dict[str, Any]) -> bool:
    """Return whether an adapter result already denies the hook operation."""
    if result.get("continue") is False:
        return True

    decision = result.get("decision")
    if isinstance(decision, str) and decision.casefold() in {"block", "deny"}:
        return True

    permission_decision = result.get("permissionDecision")
    if isinstance(permission_decision, str) and permission_decision.casefold() == "deny":
        return True

    hook_output = result.get("hookSpecificOutput")
    if isinstance(hook_output, dict):
        permission_decision = hook_output.get("permissionDecision")
        if isinstance(permission_decision, str) and permission_decision.casefold() == "deny":
            return True

    return False
