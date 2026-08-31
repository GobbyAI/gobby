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
            # Provider-specific catalogs may expose a narrower effort set.
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


# Bundled AGY 1.1.18 effort and alias table. Live availability comes from the
# capability collector; daemon callers still route by base canonical IDs and
# choose the AGY display string via reasoning_effort at the adapter boundary.
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
    "gemini-3.7-flash": {
        "value": "gemini-3.7-flash",
        "canonical_id": "gemini-3.7-flash",
        "label": "Gemini 3.7 Flash",
        "model_family": "gemini",
        "context_lookup_key": "gemini-3.7-flash",
        "context_length": 1_048_576,
        "context_length_source": "provider_catalog",
        "availability_source": "bundled",
        "effort_display": {
            "low": "Gemini 3.7 Flash (Low)",
            "medium": "Gemini 3.7 Flash (Medium)",
            "high": "Gemini 3.7 Flash (High)",
        },
        "reasoning": {
            "supported_efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
    },
    "gemini-3.6-flash": {
        "value": "gemini-3.6-flash",
        "canonical_id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash",
        "model_family": "gemini",
        "context_lookup_key": "gemini-3.6-flash",
        "context_length": 1_048_576,
        "context_length_source": "provider_catalog",
        "availability_source": "bundled",
        "effort_display": {
            "low": "Gemini 3.6 Flash (Low)",
            "medium": "Gemini 3.6 Flash (Medium)",
            "high": "Gemini 3.6 Flash (High)",
        },
        "reasoning": {
            "supported_efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
    },
    "gemini-3.5-flash": {
        "value": "gemini-3.5-flash",
        "canonical_id": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash",
        "model_family": "gemini",
        "context_lookup_key": "gemini-3.5-flash",
        "context_length": 1_048_576,
        "context_length_source": "provider_catalog",
        "availability_source": "bundled",
        "effort_display": {
            "low": "Gemini 3.5 Flash (Low)",
            "medium": "Gemini 3.5 Flash (Medium)",
            "high": "Gemini 3.5 Flash (High)",
        },
        "reasoning": {
            "supported_efforts": ["low", "medium", "high"],
            "default_effort": "medium",
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
        "availability_source": "bundled",
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
        "availability_source": "bundled",
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
        "availability_source": "bundled",
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
        "availability_source": "bundled",
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

__all__ = ["AGY_MODELS", "GEMINI_FAMILY_MODELS"]
