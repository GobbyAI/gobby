"""Classifiers for daemon/protocol prompts that are not user intent."""

from __future__ import annotations

WAKE_PROMPT_PREFIX = "Message from Gobby daemon: New activity available."
PROTOCOL_PROMPT_PREFIXES = (
    "<codex_internal_context",
    "<turn_aborted",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<environment_context>",
)


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
