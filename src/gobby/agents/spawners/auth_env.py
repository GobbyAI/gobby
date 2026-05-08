"""Environment passthrough helpers for terminal agent spawns."""

from __future__ import annotations

import os
from collections.abc import Mapping

UNIVERSAL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "CLAUDE_CONFIG_DIR",
        "SSH_AUTH_SOCK",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
    }
)

CLI_ENV_ALLOWLIST: dict[str, frozenset[str]] = {
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_MODEL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "DISABLE_AUTOUPDATER",
        }
    ),
    "codex": frozenset(
        {
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_MODEL",
        }
    ),
    "gemini": frozenset(
        {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
        }
    ),
    "qwen": frozenset(
        {
            "DASHSCOPE_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "QWEN_API_KEY",
            "QWEN_API_BASE",
        }
    ),
    "droid": frozenset(
        {
            "FACTORY_API_KEY",
            "FACTORY_BASE_URL",
            "FACTORY_API_BASE_URL",
        }
    ),
}

CLI_CREDENTIAL_KEYS: dict[str, frozenset[str]] = {
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_BEARER_TOKEN_BEDROCK",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_OPENAI_API_KEY",
        }
    ),
    "codex": frozenset({"OPENAI_API_KEY"}),
    "gemini": frozenset(
        {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    ),
    "qwen": frozenset({"DASHSCOPE_API_KEY", "OPENAI_API_KEY", "QWEN_API_KEY"}),
    "droid": frozenset({"FACTORY_API_KEY"}),
}


def terminal_env_passthrough(
    cli: str,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return allowlisted env vars present and non-empty in source."""
    env = source or os.environ
    normalized_cli = cli.lower()
    allowed = set(UNIVERSAL_ALLOWLIST)
    allowed.update(CLI_ENV_ALLOWLIST.get(normalized_cli, frozenset()))

    return {key: value for key in allowed if (value := env.get(key))}


def has_auth_env(
    cli: str,
    *,
    source: Mapping[str, str] | None = None,
) -> bool:
    """True when any credential key for the CLI is present and non-empty."""
    env = source or os.environ
    normalized_cli = cli.lower()
    credential_keys = set(CLI_CREDENTIAL_KEYS.get(normalized_cli, frozenset()))

    return any(bool(env.get(key)) for key in credential_keys)
