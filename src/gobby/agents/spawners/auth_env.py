"""Credential environment classification for agent spawns."""

from __future__ import annotations

import os
from collections.abc import Mapping

from gobby.agents.credential_inventory import CLI_DENIED_AMBIENT_KEYS
from gobby.ai.codex_endpoint import CODEX_ENDPOINT_API_KEY_ENV
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV

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

ALL_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {GOBBY_AGENT_API_TOKEN_ENV, CODEX_ENDPOINT_API_KEY_ENV}
    | {key for keys in CLI_CREDENTIAL_KEYS.values() for key in keys}
)


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


__all__ = [
    "ALL_CREDENTIAL_KEYS",
    "CLI_CREDENTIAL_KEYS",
    "CLI_DENIED_AMBIENT_KEYS",
    "has_auth_env",
    "split_credential_env",
]
