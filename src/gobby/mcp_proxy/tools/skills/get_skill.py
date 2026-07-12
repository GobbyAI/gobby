"""Handlers for the get_skill and get_skill_file tools."""

from __future__ import annotations

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)


def _serve_scan_error(
    skill_name: str,
    source_type: str | None,
    content: str,
    path: str = "SKILL.md",
) -> dict[str, Any] | None:
    """Serve-time gate: rescan external-tier content before it reaches context.

    Returns an error response dict when the content must not be served, or
    None when serving is allowed. Content hashes are cached, so each unique
    content is scanned once per process. External sources fail closed when
    the scanner is unavailable.
    """
    from gobby.skills.scanner import is_external_source, scan_served_content

    if not is_external_source(source_type):
        return None
    try:
        scan_result = scan_served_content(content, name=skill_name, path=path)
    except ImportError:
        return {
            "success": False,
            "error": (
                f"clawcare is not installed; refusing to serve skill "
                f"'{skill_name}' from external source ({source_type}) "
                f"without a security scan"
            ),
        }
    if not scan_result["is_safe"]:
        return {
            "success": False,
            "error": (
                f"Skill '{skill_name}' content at {path} failed security scan "
                f"(max severity: {scan_result['max_severity']}); refusing to serve"
            ),
            "scan_result": scan_result,
        }
    return None


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the get_skill and get_skill_file tools on the registry."""

    @registry.tool(
        name="get_skill",
        description="Get full skill content by name or ID. Returns complete skill including content, allowed_tools, etc.",
    )
    async def get_skill(
        name: str | None = None,
        skill_id: str | None = None,
        session_id: str | None = None,
        level: str | None = None,
    ) -> dict[str, Any]:
        """
        Get a skill by name or ID with full content.

        Returns all skill fields including content, allowed_tools, compatibility.
        Use this after list_skills to get the full skill when needed.

        Args:
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)
            session_id: Optional session ID (accepts #N, N, UUID, or prefix) to record skill usage
            level: Optional level for leveled skills (declared in metadata.gobby.levels);
                defaults to the skill's default_level

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
                    skill = ctx.storage.get_skill(skill_id)
                except ValueError:
                    pass

            if skill is None and name:
                skill = ctx.storage.get_by_name(name, project_id=ctx.project_id)

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            scan_error = _serve_scan_error(skill.name, skill.source_type, skill.content)
            if scan_error is not None:
                return scan_error

            # Resolve effective level for leveled skills (metadata.gobby.levels)
            gobby_meta = (skill.metadata or {}).get("gobby") or {}
            levels = gobby_meta.get("levels") if isinstance(gobby_meta, dict) else None
            if not isinstance(levels, list) or not levels:
                levels = None
            if level is not None and levels is None:
                return {
                    "success": False,
                    "error": f"Skill '{skill.name}' does not declare levels",
                }
            effective_level: str | None = None
            if levels is not None:
                if level is not None and level not in levels:
                    return {
                        "success": False,
                        "error": (
                            f"Invalid level '{level}' for skill '{skill.name}'. "
                            f"Valid levels: {', '.join(levels)}"
                        ),
                    }
                effective_level = level or gobby_meta.get("default_level") or levels[0]

            # Record skill usage against the explicit session_id, falling back to
            # the ambient wrapper session context seeded by call_tool
            resolved_session_id: str | None = None
            if not session_id:
                session_id = get_current_session_id()
            if session_id:
                try:
                    resolved_session_id = ctx.session_manager.resolve_session_reference(
                        session_id, project_id=ctx.project_id
                    )
                    ctx.session_manager.record_skills_used(resolved_session_id, [skill.name])
                except Exception as e:
                    logger.debug(f"Best-effort skill tracking failed for session {session_id}: {e}")

            content = skill.content
            if effective_level is not None:
                content = f"Active level: {effective_level}\n\n{content}"
                if resolved_session_id is not None:
                    try:
                        from gobby.workflows.state_manager import SessionVariableManager

                        var_name = f"{skill.name.replace('-', '_')}_level"
                        SessionVariableManager(ctx.db).set_variable(
                            resolved_session_id, var_name, effective_level
                        )
                    except Exception as e:
                        logger.debug(
                            f"Best-effort level variable set failed for session {session_id}: {e}"
                        )
                else:
                    logger.debug(
                        f"No session resolved; skipping {skill.name} level variable persistence"
                    )

            # Build response
            skill_data: dict[str, Any] = {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "content": content,
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
            }

            # Include file metadata if files exist
            try:
                skill_files = ctx.storage.get_skill_files(skill.id)
                if skill_files:
                    skill_data["files"] = [f.to_dict() for f in skill_files]
            except Exception as e:
                logger.debug(f"Failed to get files for skill {skill.name}: {e}")

            return {
                "success": True,
                "skill": skill_data,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_skill_file",
        description="Get a single file's content from a multi-file skill. Use after get_skill() shows available files.",
    )
    async def get_skill_file_tool(
        path: str,
        name: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Fetch a single file from a skill on demand.

        Progressive disclosure: get_skill() shows file metadata (path, type, size),
        this tool fetches the actual content for a specific file.

        Args:
            path: Relative file path within the skill (e.g. "references/api.md")
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)

        Returns:
            Dict with success status and file content
        """
        try:
            if not path:
                return {"success": False, "error": "path is required"}
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            # Resolve skill
            skill = None
            if skill_id:
                try:
                    skill = ctx.storage.get_skill(skill_id)
                except ValueError:
                    pass
            if skill is None and name:
                skill = ctx.storage.get_by_name(name, project_id=ctx.project_id)
            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            # Get the file
            skill_file = ctx.storage.get_skill_file(skill.id, path)
            if skill_file is None:
                return {"success": False, "error": f"File not found: {path}"}

            scan_error = _serve_scan_error(
                skill.name, skill.source_type, skill_file.content, path=skill_file.path
            )
            if scan_error is not None:
                return scan_error

            return {
                "success": True,
                "file": skill_file.to_dict(include_content=True),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
