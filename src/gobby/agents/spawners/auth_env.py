"""Environment passthrough helpers for terminal agent spawns."""

from __future__ import annotations

import os
from collections.abc import Mapping

from gobby.ai.codex_endpoint import CODEX_ENDPOINT_API_KEY_ENV
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV

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
    "grok": frozenset(
        {
            "XAI_API_KEY",
            "GROK_API_KEY",
            "GROK_API_BASE",
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
    "agy": frozenset(),
}

CLI_CREDENTIAL_KEYS: dict[str, frozenset[str]] = {
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_OPENAI_API_KEY",
        }
    ),
    "codex": frozenset({"OPENAI_API_KEY"}),
    "grok": frozenset({"XAI_API_KEY", "GROK_API_KEY"}),
    "qwen": frozenset({"DASHSCOPE_API_KEY", "OPENAI_API_KEY", "QWEN_API_KEY"}),
    "droid": frozenset({"FACTORY_API_KEY"}),
    "agy": frozenset(),
}

CLI_DENIED_AMBIENT_KEYS: dict[str, frozenset[str]] = {
    "agy": frozenset(
        {
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    ),
}

ALL_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {GOBBY_AGENT_API_TOKEN_ENV, CODEX_ENDPOINT_API_KEY_ENV}
    | {key for keys in CLI_CREDENTIAL_KEYS.values() for key in keys}
)


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


def split_credential_env(env: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split env into public vars and credential-class vars."""
    public_env: dict[str, str] = {}
    credential_env: dict[str, str] = {}
    for key, value in env.items():
        if key in ALL_CREDENTIAL_KEYS:
            credential_env[key] = value
        else:
            public_env[key] = value
    return public_env, credential_env


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
