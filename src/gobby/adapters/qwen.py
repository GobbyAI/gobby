"""Qwen CLI adapter for hook translation.

Qwen currently exposes Gemini-compatible hook payloads, but it remains a
distinct provider so storage, routing, and telemetry preserve Qwen identity.
"""

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.hooks.events import SessionSource


class QwenAdapter(ACPHookAdapter):
    """Adapter for Qwen CLI hook translation."""

    source = SessionSource.QWEN
