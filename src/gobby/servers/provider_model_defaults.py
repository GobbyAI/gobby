"""Static provider model defaults used when live discovery is unavailable."""

from __future__ import annotations

from typing import Any

# Mirrors `droid exec --help` from Factory Droid 0.106.0 and docs.factory.ai/cli.
DROID_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "value": "claude-opus-4-7",
        "label": "Claude Opus 4.7",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high", "xhigh", "max"],
            "default_effort": "high",
        },
    },
    {
        "value": "claude-opus-4-6",
        "label": "Claude Opus 4.6",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high", "max"],
            "default_effort": "high",
        },
    },
    {
        "value": "claude-opus-4-6-fast",
        "label": "Claude Opus 4.6 Fast Mode",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high", "max"],
            "default_effort": "high",
        },
    },
    {
        "value": "claude-opus-4-5-20251101",
        "label": "Claude Opus 4.5",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high"],
            "default_effort": "off",
        },
    },
    {
        "value": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high", "max"],
            "default_effort": "high",
        },
    },
    {
        "value": "claude-sonnet-4-5-20250929",
        "label": "Claude Sonnet 4.5",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high"],
            "default_effort": "off",
        },
    },
    {
        "value": "claude-haiku-4-5-20251001",
        "label": "Claude Haiku 4.5",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high"],
            "default_effort": "off",
        },
    },
    {
        "value": "gpt-5.4",
        "label": "GPT-5.4",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "medium",
        },
    },
    {
        "value": "gpt-5.4-fast",
        "label": "GPT-5.4 Fast Mode",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "medium",
        },
    },
    {
        "value": "gpt-5.4-mini",
        "label": "GPT-5.4 Mini",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "high",
        },
    },
    {
        "value": "gpt-5.3-codex",
        "label": "GPT-5.3-Codex",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "medium",
        },
    },
    {
        "value": "gpt-5.3-codex-fast",
        "label": "GPT-5.3-Codex Fast Mode",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "medium",
        },
    },
    {
        "value": "gpt-5.2",
        "label": "GPT-5.2",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high", "xhigh"],
            "default_effort": "low",
        },
    },
    {
        "value": "gpt-5.2-codex",
        "label": "GPT-5.2-Codex",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "medium",
        },
    },
    {
        "value": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro",
        "reasoning": {"supported_efforts": ["low", "medium", "high"], "default_effort": "high"},
    },
    {
        "value": "gemini-3-flash-preview",
        "label": "Gemini 3 Flash",
        "reasoning": {
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "high",
        },
    },
    {
        "value": "minimax-m2.7",
        "label": "Droid Core (MiniMax M2.7)",
        "reasoning": {"supported_efforts": ["high"], "default_effort": "high"},
    },
    {
        "value": "minimax-m2.5",
        "label": "Droid Core (MiniMax M2.5)",
        "reasoning": {"supported_efforts": ["low", "medium", "high"], "default_effort": "high"},
    },
    {
        "value": "kimi-k2.6",
        "label": "Droid Core (Kimi K2.6)",
        "reasoning": {"supported_efforts": ["off", "high"], "default_effort": "high"},
    },
    {
        "value": "kimi-k2.5",
        "label": "Droid Core (Kimi K2.5)",
        "reasoning": {"supported_efforts": ["off", "high"], "default_effort": "high"},
    },
    {"value": "glm-5.1", "label": "Droid Core (GLM-5.1)"},
    {"value": "glm-5", "label": "Droid Core (GLM-5)"},
    {"value": "glm-4.7", "label": "Droid Core (GLM-4.7) [Deprecated]"},
    {
        "value": "gpt-5.1-codex-max",
        "label": "GPT-5.1-Codex-Max [Deprecated]",
        "reasoning": {
            "supported_efforts": ["low", "medium", "high", "xhigh"],
            "default_effort": "medium",
        },
    },
]

__all__ = ["DROID_MODEL_CATALOG"]
