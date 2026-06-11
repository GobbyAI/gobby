"""Tests for explicit local-model detection."""

from __future__ import annotations

import pytest

from gobby.llm.local_detection import is_local_agent_definition

pytestmark = pytest.mark.unit


def test_agent_definition_detection_accepts_named_local_endpoint_provider() -> None:
    assert is_local_agent_definition(provider="local:lm-studio", model="qwen2.5-coder") is True


def test_agent_definition_detection_accepts_named_local_endpoint_model() -> None:
    assert is_local_agent_definition(provider="claude", model="local:lm-studio") is True


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("claude", "local"),
        ("lmstudio", "auto"),
        ("ollama", "llama3.1"),
        ("claude", "gpt-oss-20b"),
        ("claude", "qwen-coder-32b"),
    ],
)
def test_agent_definition_detection_rejects_legacy_local_signals(
    provider: str,
    model: str,
) -> None:
    assert is_local_agent_definition(provider=provider, model=model) is False
