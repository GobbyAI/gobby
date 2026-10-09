"""Provider capability matrix for spawned terminal agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ReasoningFlagStyle = Literal["claude-effort", "codex-config", "reasoning-effort"]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Terminal-agent capabilities that must stay consistent across spawn paths."""

    reasoning_flag: ReasoningFlagStyle | None = None
    sandbox: bool = False
    sensitive_path_enforcement: bool = False
    # The agent-mode spawn command runs the CLI headless: it reads nothing from its
    # terminal, so terminal-delivered commands (compact handoffs) cannot reach it.
    headless_spawn: bool = False


PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "claude": ProviderCapabilities(
        reasoning_flag="claude-effort",
        sandbox=True,
        sensitive_path_enforcement=True,
    ),
    "codex": ProviderCapabilities(
        reasoning_flag="codex-config",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
    "droid": ProviderCapabilities(
        reasoning_flag="reasoning-effort",
        sandbox=False,
        sensitive_path_enforcement=False,
        # ``droid exec`` (command_builder) is Droid's headless mode: ``/compress``
        # typed into the pane stays unconsumed and the handoff is lost (#22402).
        headless_spawn=True,
    ),
    "grok": ProviderCapabilities(
        reasoning_flag="reasoning-effort",
        sandbox=True,
        sensitive_path_enforcement=False,
        # ``grok --single`` (command_builder) is Grok's headless mode.
        headless_spawn=True,
    ),
    "agy": ProviderCapabilities(
        reasoning_flag="claude-effort",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
}


def provider_capabilities(provider: str) -> ProviderCapabilities:
    """Return terminal-agent capabilities for a provider."""
    return PROVIDER_CAPABILITIES.get(provider, ProviderCapabilities())


def provider_reasoning_flag(provider: str) -> ReasoningFlagStyle | None:
    """Return the CLI flag style used to emit terminal reasoning for a provider."""
    return provider_capabilities(provider).reasoning_flag


def provider_supports_terminal_reasoning(provider: str) -> bool:
    """Return whether spawned terminal reasoning can be applied for a provider."""
    return provider_reasoning_flag(provider) is not None


def provider_supports_sandbox(provider: str) -> bool:
    """Return whether Gobby has a sandbox resolver for a provider."""
    return provider_capabilities(provider).sandbox


def codex_launches_headless(
    agent_name: str | None, *, sandbox_enforced: bool, sandbox_backend: str | None
) -> bool:
    """Return whether a Codex launch runs ``codex exec`` instead of the TUI.

    Only the task-close reviewer under enforced SRT runs headless; every other
    Codex launch keeps reading its terminal.
    """
    return agent_name == "task-close-reviewer" and sandbox_enforced and sandbox_backend == "srt"


def agent_run_is_headless(agent_run: object) -> bool:
    """Return whether a spawned run's CLI exits when its turn ends.

    Such a run reads nothing from its terminal, so terminal-delivered commands and
    yielded-turn wakes cannot reach it.
    """
    provider = getattr(agent_run, "provider", None)
    if not isinstance(provider, str):
        return False
    if provider_capabilities(provider).headless_spawn:
        return True
    agent_name = getattr(agent_run, "agent_name", None)
    if provider != "codex" or agent_name != "task-close-reviewer":
        return False
    metadata = getattr(agent_run, "resume_metadata_json", None)
    sandbox = metadata.get("sandbox") if isinstance(metadata, Mapping) else None
    if not isinstance(sandbox, Mapping):
        # Unrecorded launch: refusing a TUI reviewer only forgoes one compaction,
        # while accepting a headless one ends its turn and with it the process.
        return True
    return codex_launches_headless(
        agent_name,
        sandbox_enforced=sandbox.get("enforced") is True,
        sandbox_backend=sandbox.get("backend"),
    )
