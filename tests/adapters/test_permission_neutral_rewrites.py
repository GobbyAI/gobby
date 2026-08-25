from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit

Translator = Callable[[HookResponse], dict[str, Any]]


def _translators() -> list[tuple[str, Translator]]:
    return [
        (
            "claude",
            lambda response: ClaudeCodeAdapter().translate_from_hook_response(
                response, hook_type="PreToolUse"
            ),
        ),
        (
            "codex",
            lambda response: CodexHooksAdapter().translate_from_hook_response(
                response, hook_type="PreToolUse"
            ),
        ),
        (
            "qwen",
            lambda response: QwenAdapter().translate_from_hook_response(
                response, hook_type="PreToolUse"
            ),
        ),
        (
            "grok",
            lambda response: GrokAdapter().translate_from_hook_response(
                response, hook_type="pre_tool_use"
            ),
        ),
        (
            "droid",
            lambda response: DroidAdapter().translate_from_hook_response(
                response, hook_type="PreToolUse"
            ),
        ),
        (
            "agy",
            lambda response: AgyAdapter().translate_from_hook_response(
                response, hook_type="PreToolUse"
            ),
        ),
    ]


def _find_values(value: Any, key: str) -> list[Any]:
    if isinstance(value, dict):
        found = [child for name, child in value.items() if name == key]
        return found + [item for child in value.values() for item in _find_values(child, key)]
    if isinstance(value, list):
        return [item for child in value for item in _find_values(child, key)]
    return []


@pytest.mark.parametrize(("provider", "translate"), _translators())
def test_modified_input_does_not_imply_approval(provider: str, translate: Translator) -> None:
    del provider
    rewritten = {"command": "rtk git status"}

    result = translate(HookResponse(modified_input=rewritten))

    assert "allow" not in _find_values(result, "permissionDecision")
    assert "allow" not in _find_values(result, "behavior")
    assert result.get("decision") != "allow"
    assert rewritten in _find_values(result, "updatedInput") + _find_values(result, "overwrite")


@pytest.mark.parametrize(("provider", "translate"), _translators())
def test_auto_approve_preserves_explicit_approval(provider: str, translate: Translator) -> None:
    del provider
    result = translate(HookResponse(modified_input={"command": "pwd"}, auto_approve=True))

    native_decisions = (
        _find_values(result, "permissionDecision")
        + _find_values(result, "behavior")
        + ([result["decision"]] if "decision" in result else [])
    )
    assert "allow" in native_decisions


@pytest.mark.parametrize(("provider", "translate"), _translators())
def test_explicit_permission_denial_is_preserved(provider: str, translate: Translator) -> None:
    del provider
    result = translate(HookResponse(permission_decision="deny", reason="policy"))

    native_decisions = (
        _find_values(result, "permissionDecision")
        + _find_values(result, "behavior")
        + ([result["decision"]] if "decision" in result else [])
    )
    assert "deny" in native_decisions
    assert "allow" not in native_decisions
