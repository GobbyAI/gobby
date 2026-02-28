"""
Internal MCP tools for Skill management.

Exposes functionality for:
- list_skills(): List all skills with lightweight metadata
- get_skill(): Get skill by ID or name with full content
- search_skills(): Search skills by query with relevance ranking
- update_skill(): Update an existing skill by refreshing from source
- install_skill(): Install skill from local path, GitHub URL, or ZIP archive
- remove_skill(): Remove a skill by name or ID

These tools use LocalSkillManager for storage and SkillSearch for search.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.skills.hubs.manager import HubManager
from gobby.skills.loader import SkillLoader, SkillLoadError
from gobby.skills.search import SearchFilters, SkillSearch
from gobby.skills.updater import SkillUpdater
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.skills import ChangeEvent, LocalSkillManager, SkillChangeNotifier

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

__all__ = ["create_skills_registry", "SkillsToolRegistry"]


class SkillsToolRegistry(InternalToolRegistry):
    """Registry for skill management tools with extra search attribute."""

    search: SkillSearch  # Assigned dynamically in create_skills_registry


def create_skills_registry(
    db: DatabaseProtocol,
    project_id: str | None = None,
    hub_manager: HubManager | None = None,
) -> SkillsToolRegistry:
    """
    Create a skills management tool registry.

    Args:
        db: Database connection for storage
        project_id: Optional default project scope for skill operations
        hub_manager: Optional HubManager for hub operations (list_hubs, search_hub)

    Returns:
        SkillsToolRegistry with skill management tools registered
    """
    registry = SkillsToolRegistry(
        name="gobby-skills",
        description="Skill management - list_skills, get_skill, search_skills, install_skill, update_skill, remove_skill, list_hubs, search_hub",
    )

    # Initialize change notifier and storage
    notifier = SkillChangeNotifier()
    storage = LocalSkillManager(db, notifier=notifier)

    # --- list_skills tool ---

    @registry.tool(
        name="list_skills",
        description="List all skills with lightweight metadata. Supports filtering by category and enabled status.",
    )
    async def list_skills(
        category: str | None = None,
        enabled: bool | None = None,
        include_templates: bool = False,
        limit: int = 50,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        List skills with lightweight metadata.

        Returns ~100 tokens per skill: name, description, category, tags, enabled, source.
        Does NOT include content, allowed_tools, or compatibility.

        Args:
            category: Optional category filter
            enabled: Optional enabled status filter (True/False/None for all)
            include_templates: If True, also show template skills (default False)
            limit: Maximum skills to return (default 50)

        Returns:
            Dict with success status and list of skill metadata
        """
        try:
            active_names = None
            if session_id:
                try:
                    from gobby.workflows.state_manager import SessionVariableManager

                    resolved_id = session_manager.resolve_session_reference(
                        session_id, project_id=project_id
                    )
                    sv_mgr = SessionVariableManager(db)
                    sv = sv_mgr.get_variables(resolved_id)
                    active_names = sv.get("_active_skill_names") if sv else None
                except Exception:
                    logger.debug("Failed to resolve active skill names for session %s", session_id)

            skills = storage.list_skills(
                project_id=project_id,
                category=category,
                enabled=enabled,
                # Over-fetch by 5x when filtering by active_names, since the DB
                # query doesn't know about the session-scoped allowlist and we
                # need enough candidates to fill `limit` after filtering.
                limit=limit * 5 if active_names is not None else limit,
                include_global=True,
                include_templates=include_templates,
            )

            if active_names is not None:
                active_set = set(active_names)
                skills = [s for s in skills if s.name in active_set][:limit]

            # Extract lightweight metadata only
            skill_list = []
            for skill in skills:
                # Get category and tags from metadata
                category_value = None
                tags = []
                if skill.metadata and isinstance(skill.metadata, dict):
                    skillport = skill.metadata.get("skillport", {})
                    if isinstance(skillport, dict):
                        category_value = skillport.get("category")
                        tags = skillport.get("tags", [])

                skill_list.append(
                    {
                        "id": skill.id,
                        "name": skill.name,
                        "description": skill.description,
                        "category": category_value,
                        "tags": tags,
                        "enabled": skill.enabled,
                        "source": skill.source,
                    }
                )

            return {
                "success": True,
                "count": len(skill_list),
                "skills": skill_list,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- get_skill tool ---

    # Session manager for skill usage tracking
    session_manager = LocalSessionManager(db)

    @registry.tool(
        name="get_skill",
        description="Get full skill content by name or ID. Returns complete skill including content, allowed_tools, etc.",
    )
    async def get_skill(
        name: str | None = None,
        skill_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get a skill by name or ID with full content.

        Returns all skill fields including content, allowed_tools, compatibility.
        Use this after list_skills to get the full skill when needed.

        Args:
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)
            session_id: Optional session ID (accepts #N, N, UUID, or prefix) to record skill usage

        Returns:
            Dict with success status and full skill data
        """
        try:
            # Validate input
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            # Get skill by ID or name
            skill = None
            if skill_id:
                try:
                    skill = storage.get_skill(skill_id)
                except ValueError:
                    pass

            if skill is None and name:
                skill = storage.get_by_name(name, project_id=project_id)

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            # Record skill usage when session_id is provided
            if session_id:
                try:
                    resolved_id = session_manager.resolve_session_reference(
                        session_id, project_id=project_id
                    )
                    session_manager.record_skills_used(resolved_id, [skill.name])
                except Exception:
                    pass  # Best-effort tracking; don't fail the skill lookup

            # Return full skill data
            return {
                "success": True,
                "skill": {
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                    "content": skill.content,
                    "version": skill.version,
                    "license": skill.license,
                    "compatibility": skill.compatibility,
                    "allowed_tools": skill.allowed_tools,
                    "metadata": skill.metadata,
                    "enabled": skill.enabled,
                    "source": skill.source,
                    "source_path": skill.source_path,
                    "source_type": skill.source_type,
                    "source_ref": skill.source_ref,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- search_skills tool ---

    # Initialize search and index skills
    search = SkillSearch()
    # Expose search instance on registry for testing/manual indexing
    registry.search = search

    # Upper bound for skill index queries — high enough to get all installed
    # skills without unbounded queries. Actual counts are typically < 200.
    _MAX_SKILL_INDEX = 10_000

    def _index_skills() -> None:
        """Index all skills for search."""
        skills = storage.list_skills(
            project_id=project_id,
            limit=_MAX_SKILL_INDEX,
            include_global=True,
        )
        search.index_skills(skills)

    # Index on registry creation
    _index_skills()

    # Wire up change notifier to re-index on any skill mutation
    def _on_skill_change(event: ChangeEvent) -> None:
        """Re-index skills when any skill is created, updated, or deleted."""
        _index_skills()

    notifier.add_listener(_on_skill_change)

    @registry.tool(
        name="search_skills",
        description="Search for skills by query. Returns ranked results with relevance scores. Supports filtering by category and tags.",
    )
    async def search_skills(
        query: str,
        category: str | None = None,
        tags_any: list[str] | None = None,
        tags_all: list[str] | None = None,
        top_k: int = 10,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for skills by natural language query.

        Returns ranked results with relevance scores.

        Args:
            query: Search query (required, non-empty)
            category: Optional category filter
            tags_any: Optional tags filter - match any of these tags
            tags_all: Optional tags filter - match all of these tags
            top_k: Maximum results to return (default 10)

        Returns:
            Dict with success status and ranked search results
        """
        try:
            # Validate query
            if not query or not query.strip():
                return {"success": False, "error": "Query is required and cannot be empty"}

            active_names = None
            if session_id:
                try:
                    from gobby.workflows.state_manager import SessionVariableManager

                    resolved_id = session_manager.resolve_session_reference(
                        session_id, project_id=project_id
                    )
                    sv_mgr = SessionVariableManager(db)
                    sv = sv_mgr.get_variables(resolved_id)
                    active_names = sv.get("_active_skill_names") if sv else None
                except Exception:
                    pass

            # Build filters
            filters = None
            if category or tags_any or tags_all or active_names is not None:
                filters = SearchFilters(
                    category=category,
                    tags_any=tags_any,
                    tags_all=tags_all,
                    allowed_names=active_names,
                )

            # Perform search
            results = await search.search_async(query=query, top_k=top_k, filters=filters)

            # Format results with skill metadata
            result_list = []
            for r in results:
                # Look up skill to get description, category, tags
                skill = None
                try:
                    skill = storage.get_skill(r.skill_id)
                except ValueError:
                    pass

                # Get category and tags from metadata
                category_value = None
                tags = []
                if skill and skill.metadata and isinstance(skill.metadata, dict):
                    skillport = skill.metadata.get("skillport", {})
                    if isinstance(skillport, dict):
                        category_value = skillport.get("category")
                        tags = skillport.get("tags", [])

                result_list.append(
                    {
                        "skill_id": r.skill_id,
                        "skill_name": r.skill_name,
                        "description": skill.description if skill else None,
                        "category": category_value,
                        "tags": tags,
                        "score": r.similarity,
                    }
                )

            return {
                "success": True,
                "count": len(result_list),
                "results": result_list,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- remove_skill tool ---

    @registry.tool(
        name="remove_skill",
        description="Soft-delete a skill by name or ID. The skill can be restored later with restore_skill.",
    )
    async def remove_skill(
        name: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Soft-delete a skill (sets deleted_at, can be restored).

        Args:
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)

        Returns:
            Dict with success status and removed skill info
        """
        try:
            # Validate input
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            # Find the skill first to get its name
            skill = None
            if skill_id:
                try:
                    skill = storage.get_skill(skill_id)
                except ValueError:
                    pass

            if skill is None and name:
                skill = storage.get_by_name(name, project_id=project_id)

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            # Store the name before deletion
            skill_name = skill.name

            # Soft-delete the skill (notifier triggers re-indexing automatically)
            storage.delete_skill(skill.id)

            return {
                "success": True,
                "removed": True,
                "skill_name": skill_name,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- install_from_template tool ---

    @registry.tool(
        name="install_from_template",
        description="Create an installed copy from a template skill. Templates are bundled skill definitions; installing creates an active copy.",
    )
    async def install_from_template(
        skill_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """
        Install a skill from a template.

        Args:
            skill_id: Template skill ID (takes precedence over name)
            name: Template skill name

        Returns:
            Dict with success status and installed skill info
        """
        try:
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            skill = None
            if skill_id:
                try:
                    skill = storage.get_skill(skill_id, include_deleted=True)
                except ValueError:
                    pass

            if skill is None and name:
                skill = storage.get_by_name(
                    name,
                    project_id=project_id,
                    source="template",
                    include_deleted=True,
                    include_templates=True,
                )

            if skill is None:
                return {"success": False, "error": f"Template not found: {skill_id or name}"}

            installed = storage.install_from_template(skill.id)
            return {
                "success": True,
                "installed": True,
                "skill_id": installed.id,
                "skill_name": installed.name,
                "template_id": skill.id,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- restore_skill tool ---

    @registry.tool(
        name="restore_skill",
        description="Restore a soft-deleted skill.",
    )
    async def restore_skill(
        skill_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """
        Restore a soft-deleted skill.

        Args:
            skill_id: Skill ID (takes precedence over name)
            name: Skill name

        Returns:
            Dict with success status and restored skill info
        """
        try:
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            skill = None
            if skill_id:
                try:
                    skill = storage.get_skill(skill_id, include_deleted=True)
                except ValueError:
                    pass

            if skill is None and name:
                skill = storage.get_by_name(
                    name,
                    project_id=project_id,
                    include_deleted=True,
                    include_templates=True,
                )

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            if skill.deleted_at is None:
                return {"success": False, "error": f"Skill '{skill.name}' is not deleted"}

            restored = storage.restore(skill.id)
            return {
                "success": True,
                "restored": True,
                "skill_id": restored.id,
                "skill_name": restored.name,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- move_to_project tool ---

    @registry.tool(
        name="move_skill_to_project",
        description="Move a skill to project scope.",
    )
    async def move_skill_to_project(
        skill_id: str,
        target_project_id: str,
    ) -> dict[str, Any]:
        """
        Move a skill to project scope.

        Args:
            skill_id: Skill ID to move
            target_project_id: Target project ID

        Returns:
            Dict with success status and moved skill info
        """
        try:
            skill = storage.move_to_project(skill_id, target_project_id)
            return {
                "success": True,
                "skill_id": skill.id,
                "skill_name": skill.name,
                "source": skill.source,
                "project_id": skill.project_id,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- move_to_installed tool ---

    @registry.tool(
        name="move_skill_to_installed",
        description="Move a project-scoped skill back to installed scope.",
    )
    async def move_skill_to_installed(
        skill_id: str,
    ) -> dict[str, Any]:
        """
        Move a project-scoped skill back to installed scope.

        Args:
            skill_id: Skill ID to move

        Returns:
            Dict with success status and moved skill info
        """
        try:
            skill = storage.move_to_installed(skill_id)
            return {
                "success": True,
                "skill_id": skill.id,
                "skill_name": skill.name,
                "source": skill.source,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- update_skill tool ---

    # Initialize updater
    updater = SkillUpdater(storage)

    @registry.tool(
        name="update_skill",
        description="Update a skill by refreshing from its source. Returns whether the skill was updated.",
    )
    async def update_skill(
        name: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Update a skill by refreshing from its source path.

        Args:
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)

        Returns:
            Dict with success status and update info
        """
        try:
            # Validate input
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            # Find the skill first
            skill = None
            if skill_id:
                try:
                    skill = storage.get_skill(skill_id)
                except ValueError:
                    pass

            if skill is None and name:
                skill = storage.get_by_name(name, project_id=project_id)

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            # Use SkillUpdater to refresh from source
            # (notifier triggers re-indexing automatically if updated)
            result = updater.update_skill(skill.id)

            if result.error:
                return {"success": False, "error": result.error}

            return {
                "success": True,
                "updated": result.updated,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- install_skill tool ---

    # Initialize loader
    loader = SkillLoader()

    @registry.tool(
        name="install_skill",
        description="Install a skill from a local path, GitHub URL, or ZIP archive. Auto-detects source type.",
    )
    async def install_skill(
        source: str | None = None,
        project_scoped: bool = False,
    ) -> dict[str, Any]:
        """
        Install a skill from a source location.

        Auto-detects source type:
        - Local directory or SKILL.md file
        - GitHub URL (owner/repo, github:owner/repo, https://github.com/...)
        - ZIP archive (.zip file)

        Args:
            source: Path or URL to the skill source (required)
            project_scoped: If True, install skill scoped to the project

        Returns:
            Dict with success status, skill_id, skill_name, and source_type
        """
        try:
            # Validate input
            if not source or not source.strip():
                return {"success": False, "error": "source parameter is required"}

            source = source.strip()

            # Determine source type and load skill
            from gobby.skills.parser import ParsedSkill
            from gobby.storage.skills import SkillSourceType

            parsed_skill: ParsedSkill | list[ParsedSkill] | None = None
            source_type: SkillSourceType | None = None

            # Check for hub:slug syntax (e.g., "clawdhub:commit-message")
            # Must have exactly one colon, not be a URL, and the hub part must be alphanumeric
            hub_pattern = re.compile(r"^([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)$")
            hub_match = hub_pattern.match(source)

            if hub_match and not source.startswith("http"):
                # Hub reference: hub_name:skill_slug
                hub_name, skill_slug = hub_match.groups()

                if hub_manager is None:
                    return {
                        "success": False,
                        "error": "No hub manager configured. Add hubs to config to enable hub installs.",
                    }

                if not hub_manager.has_hub(hub_name):
                    return {
                        "success": False,
                        "error": f"Unknown hub: {hub_name}. Use list_hubs to see available hubs.",
                    }

                try:
                    # Get the provider and download the skill
                    provider = hub_manager.get_provider(hub_name)
                    download_result = await provider.download_skill(skill_slug)

                    if not download_result.success or not download_result.path:
                        return {
                            "success": False,
                            "error": f"Failed to download from hub: {download_result.error or 'Unknown error'}",
                        }

                    # Load the skill from the downloaded path
                    skill_path = Path(download_result.path)
                    parsed_skill = loader.load_skill(skill_path)
                    source_type = "hub"

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to install from hub {hub_name}: {e}",
                    }

            # Check if it's a GitHub URL/reference (only if not already parsed from hub)
            is_github_ref = False
            if parsed_skill is None:
                # Pattern for owner/repo format (e.g., "anthropic/claude-code")
                # Must match owner/repo pattern without path traversal or absolute paths
                github_owner_repo_pattern = re.compile(
                    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]*)?$"
                )

                # Explicit GitHub references (always treated as GitHub, no filesystem check)
                is_explicit_github = (
                    source.startswith("github:")
                    or source.startswith("https://github.com/")
                    or source.startswith("http://github.com/")
                )

                # For implicit owner/repo patterns, check local filesystem first
                is_implicit_github_pattern = (
                    not is_explicit_github
                    and github_owner_repo_pattern.match(source)
                    and not source.startswith("/")
                    and ".." not in source  # Reject path traversal
                )

                # Determine if this is a GitHub reference:
                # - Explicit refs are always GitHub
                # - Implicit patterns are GitHub only if local path doesn't exist
                is_github_ref = is_explicit_github or bool(
                    is_implicit_github_pattern and not Path(source).exists()
                )

            if parsed_skill is None and is_github_ref:
                # GitHub URL
                try:
                    parsed_skill = loader.load_from_github(source)
                    source_type = "github"
                except SkillLoadError as e:
                    return {"success": False, "error": f"Failed to load from GitHub: {e}"}

            # Check if it's a ZIP file
            elif parsed_skill is None and source.endswith(".zip"):
                zip_path = Path(source)
                if not zip_path.exists():
                    return {"success": False, "error": f"ZIP file not found: {source}"}
                try:
                    parsed_skill = loader.load_from_zip(zip_path)
                    source_type = "zip"
                except SkillLoadError as e:
                    return {"success": False, "error": f"Failed to load from ZIP: {e}"}

            # Assume it's a local path
            elif parsed_skill is None:
                local_path = Path(source)
                if not local_path.exists():
                    return {"success": False, "error": f"Path not found: {source}"}
                try:
                    parsed_skill = loader.load_skill(local_path)
                    source_type = "local"
                except SkillLoadError as e:
                    return {"success": False, "error": f"Failed to load skill: {e}"}

            if parsed_skill is None:
                return {"success": False, "error": "Failed to load skill from source"}

            # Handle case where load_from_github/load_from_zip returns a list
            if isinstance(parsed_skill, list):
                if len(parsed_skill) == 0:
                    return {"success": False, "error": "No skills found in source"}
                # Use the first skill if multiple were found
                parsed_skill = parsed_skill[0]

            # Determine project ID for the skill
            skill_project_id = project_id if project_scoped else None

            # Store the skill
            skill = storage.create_skill(
                name=parsed_skill.name,
                description=parsed_skill.description,
                content=parsed_skill.content,
                version=parsed_skill.version,
                license=parsed_skill.license,
                compatibility=parsed_skill.compatibility,
                allowed_tools=parsed_skill.allowed_tools,
                metadata=parsed_skill.metadata,
                source_path=parsed_skill.source_path,
                source_type=source_type,
                source_ref=getattr(parsed_skill, "source_ref", None),
                project_id=skill_project_id,
                enabled=True,
            )
            # Notifier triggers re-indexing automatically via create_skill

            return {
                "success": True,
                "installed": True,
                "skill_id": skill.id,
                "skill_name": skill.name,
                "source_type": source_type,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- list_hubs tool ---

    @registry.tool(
        name="list_hubs",
        description="List all configured skill hubs. Returns hub names and types.",
    )
    async def list_hubs() -> dict[str, Any]:
        """
        List all configured skill hubs.

        Returns hub name, type, and base_url for each configured hub.

        Returns:
            Dict with success status and list of hub info
        """
        try:
            if hub_manager is None:
                return {
                    "success": True,
                    "count": 0,
                    "hubs": [],
                }

            hub_names = hub_manager.list_hubs()
            hubs_list = []

            for name in hub_names:
                config = hub_manager.get_config(name)
                hubs_list.append(
                    {
                        "name": name,
                        "type": config.type,
                        "base_url": config.base_url,
                    }
                )

            return {
                "success": True,
                "count": len(hubs_list),
                "hubs": hubs_list,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- search_hub tool ---

    @registry.tool(
        name="search_hub",
        description="Search for skills across configured hubs. Returns ranked results from all or specific hubs.",
    )
    async def search_hub(
        query: str,
        hub_name: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Search for skills across configured hubs.

        Args:
            query: Search query (required, non-empty)
            hub_name: Optional specific hub to search (None for all hubs)
            limit: Maximum results per hub (default 20)

        Returns:
            Dict with success status and search results
        """
        try:
            # Validate query
            if not query or not query.strip():
                return {"success": False, "error": "Query is required and cannot be empty"}

            if hub_manager is None:
                return {
                    "success": False,
                    "error": "No hub manager configured. Add hubs to config to enable hub search.",
                }

            # Build hub filter
            hub_names_filter = [hub_name] if hub_name else None

            # Perform search
            results = await hub_manager.search_all(
                query=query.strip(),
                limit=limit,
                hub_names=hub_names_filter,
            )

            return {
                "success": True,
                "count": len(results),
                "results": results,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    return registry
