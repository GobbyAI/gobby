"""Gemini concrete ACP client.

Thin subclass of :class:`gobby.adapters.acp_client.ACPClient` that pins the
Gemini-specific class attributes. All protocol mechanics live in the base.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

from gobby.adapters.acp_client import (
    ACP_PROMPT_TIMEOUT_ENV_GEMINI,
    DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
    DEFAULT_ACP_REQUEST_TIMEOUT_SECONDS,
    ACPClient,
    StreamEvent,
)

# Backward-compatible re-export. Older callers imported this name from this
# module before the base/concrete split.
ACP_PROMPT_TIMEOUT_ENV = ACP_PROMPT_TIMEOUT_ENV_GEMINI


class GeminiACPClient(ACPClient):
    """ACP client for the Gemini CLI."""

    cli_name: ClassVar[str] = "gemini"
    display_name: ClassVar[str] = "Gemini"
    prompt_timeout_env: ClassVar[str] = ACP_PROMPT_TIMEOUT_ENV_GEMINI
    required_env: ClassVar[Mapping[str, str]] = MappingProxyType({"GEMINI_CLI_NO_RELAUNCH": "true"})


__all__ = [
    "ACP_PROMPT_TIMEOUT_ENV",
    "DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS",
    "DEFAULT_ACP_REQUEST_TIMEOUT_SECONDS",
    "GeminiACPClient",
    "StreamEvent",
]
