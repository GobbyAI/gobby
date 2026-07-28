"""Static provider model defaults used when live discovery is unavailable."""

from __future__ import annotations

from typing import Any

# Gemini-family catalog metadata is retained for providers that expose Gemini
# model families, currently AGY and Droid. It is not an active Gemini provider.
GEMINI_FAMILY_MODELS: list[dict[str, Any]] = [
    {
        "value": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash",
        "reasoning": {
            "supported_efforts": ["minimal", "low", "medium", "high"],
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
]


# Static AGY 1.0.10 text-generation catalog. AGY accepts model display strings on
# the CLI, while daemon callers route by base canonical IDs and choose the AGY
# display string via reasoning_effort at the adapter boundary.
def _agy_model_entry(entry: dict[str, Any]) -> dict[str, Any]:
    reasoning = entry.get("reasoning")
    if not isinstance(reasoning, dict):
        raise ValueError(f"AGY model {entry.get('value')} is missing reasoning metadata")
    supported_efforts = reasoning.get("supported_efforts")
    if not isinstance(supported_efforts, list) or not supported_efforts:
        raise ValueError(f"AGY model {entry.get('value')} has invalid supported_efforts")
    if not all(isinstance(effort, str) for effort in supported_efforts):
        raise ValueError(f"AGY model {entry.get('value')} has non-string supported_efforts")

    effort_display = entry.get("effort_display")
    if not isinstance(effort_display, dict):
        raise ValueError(f"AGY model {entry.get('value')} is missing effort_display")

    supported_set = set(supported_efforts)
    display_set = set(effort_display)
    if display_set != supported_set:
        raise ValueError(
            f"AGY model {entry.get('value')} effort_display keys {sorted(display_set)} "
            f"do not match supported efforts {sorted(supported_set)}"
        )

    return {
        **entry,
        "effort_display": {effort: effort_display[effort] for effort in supported_efforts},
    }


AGY_MODELS: dict[str, dict[str, Any]] = {
    "gemini-3.5-flash": {
        "value": "gemini-3.5-flash",
        "canonical_id": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash",
        "model_family": "gemini",
        "context_lookup_key": "gemini-3.5-flash",
        "context_length": 1_048_576,
        "context_length_source": "provider_catalog",
        "availability_source": "agy-1.0.10-static",
        "effort_display": {
            "low": "Gemini 3.5 Flash (Low)",
            "medium": "Gemini 3.5 Flash (Medium)",
            "high": "Gemini 3.5 Flash (High)",
        },
        "reasoning": {
            "supported_efforts": ["low", "medium", "high"],
            "default_effort": "low",
        },
    },
    "gemini-3.1-pro": {
        "value": "gemini-3.1-pro",
        "canonical_id": "gemini-3.1-pro",
        "label": "Gemini 3.1 Pro",
        "model_family": "gemini",
        "context_lookup_key": "gemini-3.1-pro-preview",
        "context_length": 1_000_000,
        "context_length_source": "provider_catalog",
        "availability_source": "agy-1.0.10-static",
        "effort_display": {
            "low": "Gemini 3.1 Pro (Low)",
            "high": "Gemini 3.1 Pro (High)",
        },
        "reasoning": {
            "supported_efforts": ["low", "high"],
            "default_effort": "high",
        },
    },
    "claude-sonnet-4-6": {
        "value": "claude-sonnet-4-6",
        "canonical_id": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "model_family": "claude",
        "context_lookup_key": "claude-sonnet-4-6",
        "context_length": 200_000,
        "context_length_source": "provider_catalog",
        "availability_source": "agy-1.0.10-static",
        # AGY exposes a single thinking-mode variant; key it on the standard
        # "high" effort (display string unchanged) so the natural request
        # reasoning_effort="high" resolves instead of being rejected.
        "effort_display": {
            "high": "Claude Sonnet 4.6 (Thinking)",
        },
        "reasoning": {
            "supported_efforts": ["high"],
            "default_effort": "high",
        },
    },
    "claude-opus-4-6": {
        "value": "claude-opus-4-6",
        "canonical_id": "claude-opus-4-6",
        "label": "Claude Opus 4.6",
        "model_family": "claude",
        "context_lookup_key": "claude-opus-4-6",
        "context_length": 1_000_000,
        "context_length_source": "provider_catalog",
        "availability_source": "agy-1.0.10-static",
        # See claude-sonnet-4-6: thinking-mode keyed on "high".
        "effort_display": {
            "high": "Claude Opus 4.6 (Thinking)",
        },
        "reasoning": {
            "supported_efforts": ["high"],
            "default_effort": "high",
        },
    },
    "gpt-oss-120b": {
        "value": "gpt-oss-120b",
        "canonical_id": "gpt-oss-120b",
        "label": "GPT-OSS 120B",
        "model_family": "gpt-oss",
        "context_lookup_key": "gpt-oss-120b",
        "availability_source": "agy-1.0.10-static",
        "context_length": 131_072,
        "context_length_source": "provider_catalog",
        "effort_display": {
            "medium": "GPT-OSS 120B (Medium)",
        },
        "reasoning": {
            "supported_efforts": ["medium"],
            "default_effort": "medium",
        },
    },
}
AGY_MODELS = {model_id: _agy_model_entry(entry) for model_id, entry in AGY_MODELS.items()}

# Model metadata is captured from `droid exec --help`; GLM-5.2 was refreshed against
# Factory Droid 0.180.0.
DROID_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "value": "claude-fable-5",
        "label": "Claude Fable 5",
        "reasoning": {
            "supported_efforts": ["off", "low", "medium", "high", "xhigh", "max"],
            "default_effort": "high",
        },
    },
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
    *GEMINI_FAMILY_MODELS,
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
    {
        "value": "glm-5.2",
        "label": "Droid Core (GLM-5.2)",
        "reasoning": {
            "supported_efforts": ["off", "high", "max"],
            "default_effort": "high",
        },
    },
    {
        "value": "glm-5.2-fast",
        "label": "Droid Core (GLM-5.2 Fast)",
        "reasoning": {
            "supported_efforts": ["off", "high", "max"],
            "default_effort": "high",
        },
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

__all__ = ["AGY_MODELS", "DROID_MODEL_CATALOG", "GEMINI_FAMILY_MODELS"]
