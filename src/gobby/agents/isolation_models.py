"""Shared models for agent isolation handlers."""

from __future__ import annotations

import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class IsolationContext:
    """Result of environment preparation."""

    cwd: str
    branch_name: str | None = None
    worktree_id: str | None = None
    clone_id: str | None = None
    isolation_type: Literal["none", "worktree", "clone"] = "none"
    extra: dict[str, Any] = field(default_factory=dict)


_INVALID_GIT_REF_CHARS = re.compile(r"[\000-\037\177 ~^:?*\[\\{}]+")


def _sanitize_branch_name(value: str) -> str:
    ref = value.strip().replace("@{", "-")
    ref = _INVALID_GIT_REF_CHARS.sub("-", ref)
    while ".." in ref:
        ref = ref.replace("..", "-")
    ref = re.sub(r"/+", "/", ref)
    ref = ref.strip("./-")
    while ref.endswith(".lock"):
        ref = ref[: -len(".lock")].rstrip("./-")
    return ref or f"agent/{int(time.time())}-{uuid.uuid4().hex[:8]}"


@dataclass
class SpawnConfig:
    """Configuration passed to isolation handlers."""

    prompt: str
    task_id: str | None
    task_title: str | None
    task_seq_num: int | None
    branch_name: str | None
    branch_prefix: str | None
    base_branch: str
    project_id: str
    project_path: str
    provider: str
    parent_session_id: str


def generate_branch_name(config: SpawnConfig) -> str:
    """
    Auto-generate branch name from task or fallback to prefix+timestamp.

    Priority:
    1. Explicit branch_name if provided
    2. task-{seq_num}-{slugified_title} if task info available
    3. {branch_prefix}{timestamp} as fallback (default prefix: "agent/")
    """
    if config.branch_name:
        return _sanitize_branch_name(config.branch_name)

    if config.task_seq_num and config.task_title:
        # Generate slug from task title
        slug = config.task_title.lower().replace(" ", "-")
        # Keep only alphanumeric and hyphens
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
        slug = "-".join(part for part in slug.split("-") if part)
        # Truncate to 40 chars
        slug = slug[:40]
        suffix = f"-{slug}" if slug else ""
        return _sanitize_branch_name(f"task-{config.task_seq_num}{suffix}")

    # Fallback to prefix + timestamp
    prefix = config.branch_prefix or "agent/"
    return _sanitize_branch_name(f"{prefix}{int(time.time())}-{uuid.uuid4().hex[:8]}")


class IsolationHandler(ABC):
    """Abstract base class for isolation handlers."""

    @abstractmethod
    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """
        Prepare isolated environment (worktree/clone creation).

        Args:
            config: Spawn configuration with project and task info

        Returns:
            IsolationContext with cwd and isolation metadata
        """

    @abstractmethod
    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """
        Clean up partially created environment after prepare_environment failure.

        Handlers track what was created during prepare_environment.
        This method reverses those partial side effects.

        Args:
            config: The same SpawnConfig passed to prepare_environment
        """

    @abstractmethod
    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """
        Build prompt with isolation context warnings.

        Args:
            original_prompt: The original user prompt
            ctx: Isolation context from prepare_environment

        Returns:
            Enhanced prompt with isolation context (or unchanged for current)
        """
