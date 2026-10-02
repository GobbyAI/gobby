"""Tests for terminal agent auth environment classification."""

from __future__ import annotations

import pytest

from gobby.agents.spawners.auth_env import has_auth_env, split_credential_env

pytestmark = pytest.mark.unit


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


def test_unknown_cli_has_no_auth_env() -> None:
    assert has_auth_env("unknown", source={"OPENAI_API_KEY": "sk-openai"}) is False


def test_claude_oauth_token_is_not_auth_env() -> None:
    assert has_auth_env("claude", source={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"}) is False


def test_agy_credentials_are_explicitly_empty() -> None:
    from gobby.agents.spawners.auth_env import CLI_CREDENTIAL_KEYS

    assert "agy" in CLI_CREDENTIAL_KEYS
    assert CLI_CREDENTIAL_KEYS["agy"] == frozenset()


def test_sandbox_masking_uses_the_shared_agy_denied_inventory() -> None:
    from gobby.agents.credential_inventory import CLI_DENIED_AMBIENT_KEYS as SHARED_DENIED
    from gobby.agents.sandbox_policy import _PROVIDER_CREDENTIAL_ENV, credential_env_vars
    from gobby.agents.spawners.auth_env import CLI_DENIED_AMBIENT_KEYS

    expected = frozenset({"GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"})
    assert CLI_DENIED_AMBIENT_KEYS is SHARED_DENIED
    assert CLI_DENIED_AMBIENT_KEYS["agy"] == expected
    assert frozenset(_PROVIDER_CREDENTIAL_ENV["agy"]) == expected
    assert _PROVIDER_CREDENTIAL_ENV["agy"] == tuple(sorted(expected))

    masked = credential_env_vars("agy", None)
    assert {entry.name for entry in masked} == expected
    assert {entry.mode for entry in masked} == {"mask"}
