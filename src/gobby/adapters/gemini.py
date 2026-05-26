"""Gemini CLI adapter for hook translation."""

from __future__ import annotations

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.hooks.events import SessionSource


class GeminiAdapter(ACPHookAdapter):
    """Adapter for Gemini CLI hook translation."""

    source = SessionSource.GEMINI


__all__ = ["GeminiAdapter"]
