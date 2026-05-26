"""
Internal MCP tools for Skill management.

Registry builder that assembles per-handler modules into a single
SkillsToolRegistry. Each tool handler lives in its own file within
this package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills import (
    get_skill,
    hub_tools,
    install_skill,
    list_skills,
    move_skill,
    remove_skill,
    restore_skill,
    search_skills,
    update_skill,
)
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.search import SearchConfig
from gobby.skills.hubs.manager import HubManager
from gobby.skills.loader import SkillLoader
from gobby.skills.search import SkillSearch
from gobby.skills.updater import SkillUpdater
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillChangeNotifier

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

__all__ = ["create_skills_registry", "SkillsToolRegistry"]


class SkillsToolRegistry(InternalToolRegistry):
    """Registry for skill management tools with extra search attribute."""

    search: SkillSearch  # Assigned dynamically in create_skills_registry


def create_skills_registry(
    db: HubDatabase,
    project_id: str | None = None,
    hub_manager: HubManager | None = None,
    search_config: SearchConfig | None = None,
    embedding_model: str = "nomic-embed-text",
    embedding_api_base: str | None = None,
    embedding_api_key: str | None = None,
    embedding_dim: int | None = None,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> SkillsToolRegistry:
    """
    Create a skills management tool registry.

    Args:
        db: Database connection for storage
        project_id: Optional default project scope for skill operations
        hub_manager: Optional HubManager for hub operations (list_hubs, search_hub)
        search_config: Optional search configuration
        embedding_model: Embedding model name (from EmbeddingsConfig)
        embedding_api_base: API base URL for embedding endpoint
        embedding_api_key: API key for embedding provider
        embedding_dim: Expected embedding dimension. When set, mismatches fail fast.
        run_db: Optional bounded executor bridge for blocking database calls.

    Returns:
        SkillsToolRegistry with skill management tools registered
    """
    registry = SkillsToolRegistry(
        name="gobby-skills",
        description="Skill management - list_skills, get_skill, search_skills, install_skill, update_skill, remove_skill, list_hubs, search_hub",
    )

    # Initialize shared dependencies (single instances, matching original wiring)
    notifier = SkillChangeNotifier()
    storage = LocalSkillManager(db, notifier=notifier)

    ctx = SkillsContext(
        db=db,
        storage=storage,
        notifier=notifier,
        session_manager=SessionManager(db),
        search=SkillSearch(
            config=search_config,
            db=db,
            embedding_model=embedding_model,
            embedding_api_base=embedding_api_base,
            embedding_api_key=embedding_api_key,
            embedding_dim=embedding_dim,
        ),
        updater=SkillUpdater(storage),
        loader=SkillLoader(),
        project_id=project_id,
        hub_manager=hub_manager,
        db_runner=run_db,
    )

    # Expose search instance on registry for testing/manual indexing
    registry.search = ctx.search

    # Register all tool handlers
    list_skills.register(ctx, registry)
    get_skill.register(ctx, registry)
    search_skills.register(ctx, registry)
    remove_skill.register(ctx, registry)
    restore_skill.register(ctx, registry)
    move_skill.register(ctx, registry)
    update_skill.register(ctx, registry)
    install_skill.register(ctx, registry)
    hub_tools.register(ctx, registry)

    return registry
