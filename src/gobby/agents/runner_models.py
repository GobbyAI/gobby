"""
Data models for the agent runner.

Extracted from runner.py as part of Strangler Fig decomposition (Wave 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.llm.executor import ToolSchema
from gobby.storage.agents import AgentRun
from gobby.storage.session_models import Session


@dataclass
class AgentConfig:
    """Configuration for running an agent."""

    prompt: str
    """The prompt/task for the agent to perform."""

    # Required context - can be inferred from get_project_context() or passed explicitly
    parent_session_id: str | None = None
    """ID of the session spawning this agent. Inferred from context if not provided."""

    project_id: str | None = None
    """Project ID for the agent's session. Inferred from context if not provided."""

    machine_id: str | None = None
    """Machine identifier. Defaults to hostname if not provided."""

    source: str = "claude"
    """CLI source (claude, gemini, codex, cursor, windsurf, copilot)."""

    # New spec-aligned parameters
    workflow: str | None = None
    """Workflow name or path to execute."""

    task: str | None = None
    """Task ID or 'next' for auto-select."""

    agent: str | None = None
    """Named agent definition to use."""

    lifecycle_variables: dict[str, Any] | None = None
    """Lifecycle variables to override parent settings."""

    default_variables: dict[str, Any] | None = None
    """Default variables for the agent."""

    session_context: str = "summary_markdown"
    """Context source: summary_markdown, compact_markdown, session_id:<id>, transcript:<n>, file:<path>."""

    mode: str = "in_process"
    """Execution mode: in_process, terminal, embedded, headless."""

    worktree_id: str | None = None
    """Existing worktree to use for terminal mode."""

    # Provider settings
    provider: str = "claude"
    """LLM provider to use."""

    model: str | None = None
    """Optional model override."""

    # Execution limits
    max_turns: int = 10
    """Maximum number of turns."""

    timeout: float = 120.0
    """Execution timeout in seconds."""

    system_prompt: str | None = None
    """Optional system prompt override."""

    tools: list[ToolSchema] | None = None
    """Optional list of tools to provide."""

    git_branch: str | None = None
    """Git branch for the session."""

    title: str | None = None
    """Optional title for the agent session."""

    project_path: str | None = None
    """Project path for loading project-specific workflows."""

    context_injected: bool = False
    """Whether context was successfully injected into the prompt."""

    def get_effective_workflow(self) -> str | None:
        """Get the workflow name."""
        return self.workflow


@dataclass
class AgentRunContext:
    """
    Runtime context for an agent execution.

    Contains all the objects needed to execute an agent, created during
    the prepare phase and used during execution.
    """

    session: Session | None = None
    """Child session object created for this agent."""

    run: AgentRun | None = None
    """Agent run record from the database."""

    # Convenience accessors for IDs
    @property
    def session_id(self) -> str | None:
        """Get the child session ID."""
        return self.session.id if self.session else None

    @property
    def run_id(self) -> str | None:
        """Get the agent run ID."""
        return self.run.id if self.run else None
