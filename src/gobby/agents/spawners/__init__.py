"""Terminal spawner implementations for agent execution.

This package provides the base types, command building, and prompt management.
The primary terminal spawner (:class:`TmuxSpawner`) lives in
:mod:`gobby.agents.tmux.spawner` and is lazily imported here to avoid circular
imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.agents.tmux.spawner import TmuxSpawner as TmuxSpawner

from gobby.agents.spawners.base import (
    SpawnMode,
    SpawnResult,
    TerminalSpawnerBase,
    make_spawn_env,
)
from gobby.agents.spawners.command_builder import (
    build_cli_command,
    build_codex_command_with_resume,
    build_gemini_command_with_resume,
)
from gobby.agents.spawners.prompt_manager import (
    MAX_ENV_PROMPT_LENGTH,
    create_prompt_file,
)

__all__ = [
    # Base types
    "SpawnMode",
    "SpawnResult",
    "TerminalSpawnerBase",
    "make_spawn_env",
    # TmuxSpawner (lazy)
    "TmuxSpawner",
    # Command building
    "build_cli_command",
    "build_gemini_command_with_resume",
    "build_codex_command_with_resume",
    # Prompt management
    "MAX_ENV_PROMPT_LENGTH",
    "create_prompt_file",
]


def __getattr__(name: str) -> Any:
    """Lazy import for TmuxSpawner to avoid circular imports."""
    if name == "TmuxSpawner":
        from gobby.agents.tmux.spawner import TmuxSpawner

        return TmuxSpawner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
