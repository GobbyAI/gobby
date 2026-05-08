"""Qwen concrete ACP client.

Thin subclass of :class:`gobby.adapters.acp_client.ACPClient` that pins the
Qwen-specific class attributes. Inherits all protocol mechanics from the base.
"""

from __future__ import annotations

from typing import ClassVar

from gobby.adapters.acp_client import ACP_PROMPT_TIMEOUT_ENV_QWEN, ACPClient


class QwenACPClient(ACPClient):
    """ACP client for the Qwen CLI."""

    cli_name: ClassVar[str] = "qwen"
    display_name: ClassVar[str] = "Qwen"
    prompt_timeout_env: ClassVar[str] = ACP_PROMPT_TIMEOUT_ENV_QWEN


__all__ = ["QwenACPClient"]
