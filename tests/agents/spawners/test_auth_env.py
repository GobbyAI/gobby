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


def test_agy_allowlist_and_credentials_are_explicitly_empty() -> None:
    from gobby.agents.sandbox_policy import _PROVIDER_CREDENTIAL_ENV
    from gobby.agents.spawners.auth_env import CLI_CREDENTIAL_KEYS, CLI_ENV_ALLOWLIST
    from gobby.agents.tmux.spawner import _SUPPORTED_AUTH_CLIS

    assert "agy" in CLI_ENV_ALLOWLIST
    assert CLI_ENV_ALLOWLIST["agy"] == frozenset()
    assert "agy" in CLI_CREDENTIAL_KEYS
    assert CLI_CREDENTIAL_KEYS["agy"] == frozenset()
    assert frozenset(_PROVIDER_CREDENTIAL_ENV["agy"]) == CLI_CREDENTIAL_KEYS["agy"]
    assert "agy" not in _SUPPORTED_AUTH_CLIS


def test_agy_passthrough_strips_ignored_google_ambient_keys() -> None:
    from gobby.agents.spawners import auth_env as auth_env_mod
    from gobby.agents.tmux.spawner import tmux_spawn_shell_and_env

    source = {
        "HOME": "/home/tester",
        "PATH": "/usr/bin",
        "GOOGLE_API_KEY": "google-secret",
        "GEMINI_API_KEY": "gemini-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/creds.json",
        "UNRELATED_SECRET": "nope",
    }
    result = terminal_env_passthrough("agy", source=source)
    assert result == {"HOME": "/home/tester", "PATH": "/usr/bin"}
    assert has_auth_env("agy", source=source) is False
    denied = getattr(auth_env_mod, "CLI_DENIED_AMBIENT_KEYS", None)
    assert denied is not None
    assert denied["agy"] == frozenset(
        {"GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"}
    )

    shell_cmd, extra_env = tmux_spawn_shell_and_env(
        ["agy", "--dangerously-skip-permissions"],
        {"HOME": "/home/tester", "PATH": "/usr/bin"},
        "agy",
    )
    unset_clause = shell_cmd.split(";", 1)[0]
    assert "GOOGLE_API_KEY" not in extra_env or extra_env["GOOGLE_API_KEY"] == ""
    assert "GOOGLE_API_KEY" in unset_clause
    assert "GEMINI_API_KEY" in unset_clause
    assert "GOOGLE_APPLICATION_CREDENTIALS" in unset_clause

