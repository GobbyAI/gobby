"""No-op isolation handler."""

from __future__ import annotations

from gobby.agents.isolation_models import IsolationContext, IsolationHandler, SpawnConfig


class NoneIsolationHandler(IsolationHandler):
    """
    No isolation - work in current directory.

    This is the simplest handler that just returns the project path
    as the working directory without any git branch changes.
    """

    async def prepare_environment(self, config: SpawnConfig) -> IsolationContext:
        """Return project path as working directory."""
        return IsolationContext(
            cwd=config.project_path,
            isolation_type="none",
        )

    async def cleanup_environment(self, config: SpawnConfig) -> None:
        """No-op - nothing to clean up for current directory."""

    def build_context_prompt(self, original_prompt: str, ctx: IsolationContext) -> str:
        """Return prompt unchanged - no additional context needed."""
        return original_prompt
