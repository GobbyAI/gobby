"""Tests for terminal agent auth environment passthrough."""

from __future__ import annotations

import pytest

from gobby.agents.spawners.auth_env import (
    has_auth_env,
    split_credential_env,
    terminal_env_passthrough,
)

pytestmark = pytest.mark.unit


def test_terminal_env_passthrough_returns_cli_and_universal_values() -> None:
    source = {
        "HOME": "/home/tester",
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-test",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
        "UNRELATED_SECRET": "nope",
    }

    result = terminal_env_passthrough("claude", source=source)

    assert result == {
        "HOME": "/home/tester",
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-test",
    }


def test_terminal_env_passthrough_filters_empty_values() -> None:
    source = {
        "HOME": "",
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": "https://example.test",
    }

    assert terminal_env_passthrough("codex", source=source) == {
        "OPENAI_BASE_URL": "https://example.test"
    }


def test_split_credential_env_separates_provider_secrets() -> None:
    public_env, credential_env = split_credential_env(
        {
            "GOBBY_SESSION_ID": "session-123",
            "GOBBY_AGENT_API_TOKEN": "scoped-agent-token",
            "ANTHROPIC_BASE_URL": "https://api.example.test",
            "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
            "GOBBY_CODEX_ENDPOINT_API_KEY": "endpoint-token",
            "QWEN_API_KEY": "qwen-token",
            "XAI_API_KEY": "xai-token",
            "FACTORY_API_KEY": "factory-token",
        }
    )

    assert public_env == {
        "GOBBY_SESSION_ID": "session-123",
        "ANTHROPIC_BASE_URL": "https://api.example.test",
    }
    assert credential_env == {
        "GOBBY_AGENT_API_TOKEN": "scoped-agent-token",
        "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
        "GOBBY_CODEX_ENDPOINT_API_KEY": "endpoint-token",
        "QWEN_API_KEY": "qwen-token",
        "XAI_API_KEY": "xai-token",
        "FACTORY_API_KEY": "factory-token",
    }


def test_unknown_cli_gets_universal_values_only() -> None:
    source = {
        "HOME": "/home/tester",
        "OPENAI_API_KEY": "sk-openai",
        "UNRELATED_SECRET": "nope",
    }

    assert terminal_env_passthrough("unknown", source=source) == {"HOME": "/home/tester"}
    assert has_auth_env("unknown", source=source) is False


def test_claude_oauth_token_is_never_forwarded() -> None:
    source = {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
    }

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in terminal_env_passthrough("claude", source=source)
    assert has_auth_env("claude", source=source) is False
