"""Classifiers for daemon/protocol prompts that are not user intent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

WAKE_PROMPT_PREFIX = "Message from Gobby daemon: New activity available."
SYSTEM_ACTIVITY_SOURCES = frozenset({"daemon", "system", "pipeline", "gobby_build", "build"})
SYNTHETIC_BOOLEAN_KEYS = frozenset({"synthetic", "_synthetic", "is_synthetic"})
SYNTHETIC_METADATA_KEYS = frozenset(
    {
        "actor",
        "kind",
        "message_kind",
        "origin",
        "prompt_kind",
        "prompt_origin",
        "prompt_source",
        "prompt_type",
        "role",
        "session_type",
        "source",
        "type",
    }
)
SYNTHETIC_METADATA_VALUES = SYSTEM_ACTIVITY_SOURCES | frozenset(
    {
        "assistant",
        "bootstrap",
        "control",
        "daemon_wake",
        "developer",
        "gobby",
        "gobby_daemon",
        "hook",
        "internal",
        "protocol",
        "resume",
        "system_protocol",
        "tool",
        "wait",
        "workflow",
    }
)
PROTOCOL_PROMPT_PREFIXES = (
    "<codex_internal_context",
    "<turn_aborted",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<environment_context>",
)


def synthetic_prompt_reason(
    metadata: Mapping[str, Any],
    data: Mapping[str, Any],
    prompt: str,
) -> str | None:
    """Return the synthetic-prompt reason for hook metadata/data/body."""
    for key in SYNTHETIC_BOOLEAN_KEYS:
        if metadata.get(key) or data.get(key):
            return f"metadata_{key}"

    for key in SYNTHETIC_METADATA_KEYS:
        reason = synthetic_metadata_reason(key, metadata.get(key))
        if reason is not None:
            return reason
        reason = synthetic_metadata_reason(key, data.get(key))
        if reason is not None:
            return reason

    return synthetic_body_reason(prompt)


def synthetic_metadata_reason(key: str, value: Any) -> str | None:
    """Return a synthetic-prompt reason for a single metadata value."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in SYNTHETIC_METADATA_VALUES:
        return f"metadata_{key}_{normalized}"
    return None


def synthetic_body_reason(prompt: str) -> str | None:
    """Return a synthetic-prompt reason based on prompt text only."""
    stripped = prompt.strip()
    lowered = stripped.lower()
    if not stripped:
        return "empty_prompt"
    if stripped.startswith(WAKE_PROMPT_PREFIX) or stripped.startswith("Message from Gobby daemon:"):
        return "daemon_wake_prompt"
    if lowered.startswith("agents.md instructions for ") or lowered.startswith(
        "# agents.md instructions for "
    ):
        return "agents_md_instructions"
    if lowered.startswith(PROTOCOL_PROMPT_PREFIXES):
        return "protocol_prompt"
    if looks_like_codex_bootstrap_prompt(stripped):
        return "codex_bootstrap_prompt"
    if looks_like_wait_directive(stripped):
        return "wait_directive"
    return None


def looks_like_codex_bootstrap_prompt(prompt: str) -> bool:
    """Return whether a prompt is Codex startup context rather than user intent."""
    return (
        "<permissions instructions>" in prompt
        and "<collaboration_mode>" in prompt
        and "Gobby Session ID:" in prompt
        and "## Instructions" in prompt
    )


def looks_like_wait_directive(prompt: str) -> bool:
    """Return whether a prompt is an internal wait/continue directive."""
    return (
        prompt.startswith("Continue where you last left off.")
        and "gobby-sessions.wait_for_summary" in prompt
        and "`completed=false`" in prompt
    ) or (
        prompt.startswith("Task ")
        and " has incomplete subtasks. Use suggest_next_task() and continue working." in prompt
    )
