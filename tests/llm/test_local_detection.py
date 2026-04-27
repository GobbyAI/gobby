"""Red tests for authoritative local-model detection."""

from __future__ import annotations

import pytest

from gobby.llm.local_detection import is_local_agent_definition, is_local_legacy_fallback

pytestmark = pytest.mark.unit


def test_legacy_row_detection_uses_provider_and_known_local_model_signals() -> None:
    assert is_local_legacy_fallback(provider="lmstudio", model="qwen2.5-coder") is True
    assert is_local_legacy_fallback(provider="ollama", model="llama3.1") is True
    assert is_local_legacy_fallback(provider="claude", model="gpt-oss-20b") is True


def test_legacy_row_detection_does_not_treat_literal_local_as_authoritative() -> None:
    assert is_local_legacy_fallback(provider="claude", model="local") is False
    assert is_local_legacy_fallback(provider="claude", model="qwen-coder-32b") is False


def test_agent_definition_detection_accepts_literal_local_alias() -> None:
    assert is_local_agent_definition(provider="claude", model="local") is True
    assert is_local_agent_definition(provider="lmstudio", model="auto") is True
    assert is_local_agent_definition(provider="claude", model="gpt-oss-20b") is True
