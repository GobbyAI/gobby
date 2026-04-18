"""Handler for the install_skill tool."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.skills.loader import SkillLoadError

logger = logging.getLogger(__name__)


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the install_skill tool on the registry."""

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

                if ctx.hub_manager is None:
                    return {
                        "success": False,
                        "error": "No hub manager configured. Add hubs to config to enable hub installs.",
                    }

                if not ctx.hub_manager.has_hub(hub_name):
                    return {
                        "success": False,
                        "error": f"Unknown hub: {hub_name}. Use list_hubs to see available hubs.",
                    }

                try:
                    # Get the provider and download the skill
                    provider = ctx.hub_manager.get_provider(hub_name)
                    download_result = await provider.download_skill(skill_slug)

                    if not download_result.success or not download_result.path:
                        return {
                            "success": False,
                            "error": f"Failed to download from hub: {download_result.error or 'Unknown error'}",
                        }

                    # Load the skill from the downloaded path — skip dir name
                    # check since hub downloads use temp directories
                    skill_path = Path(download_result.path)
                    parsed_skill = ctx.loader.load_skill(skill_path, check_dir_name=False)
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
                    parsed_skill = ctx.loader.load_from_github(source)
                    source_type = "github"
                except SkillLoadError as e:
                    return {"success": False, "error": f"Failed to load from GitHub: {e}"}

            # Check if it's a ZIP file
            elif parsed_skill is None and source.endswith(".zip"):
                zip_path = Path(source)
                if not zip_path.exists():
                    return {"success": False, "error": f"ZIP file not found: {source}"}
                try:
                    parsed_skill = ctx.loader.load_from_zip(zip_path)
                    source_type = "zip"
                except SkillLoadError as e:
                    return {"success": False, "error": f"Failed to load from ZIP: {e}"}

            # Assume it's a local path
            elif parsed_skill is None:
                local_path = Path(source)
                if not local_path.exists():
                    return {"success": False, "error": f"Path not found: {source}"}
                try:
                    parsed_skill = ctx.loader.load_skill(local_path)
                    source_type = "local"
                except SkillLoadError as e:
                    return {"success": False, "error": f"Failed to load skill: {e}"}

            if parsed_skill is None:
                return {"success": False, "error": "Failed to load skill from source"}

            # Handle case where load_from_github/load_from_zip returns a list
            if isinstance(parsed_skill, list):
                if len(parsed_skill) == 0:
                    return {"success": False, "error": "No skills found in source"}
                if len(parsed_skill) > 1:
                    logger.warning(
                        f"Multiple skills found ({len(parsed_skill)}), using first: "
                        f"{parsed_skill[0].name}"
                    )
                parsed_skill = parsed_skill[0]

            # Scan skill content for safety before persisting
            try:
                from gobby.skills.scanner import scan_skill_content

                scan_result = scan_skill_content(
                    content=parsed_skill.content,
                    name=parsed_skill.name,
                )
                if not scan_result["is_safe"]:
                    findings_summary = "; ".join(
                        f"[{f['severity']}] {f['title']}"
                        for f in scan_result["findings"]
                        if f["severity"] in ("HIGH", "CRITICAL")
                    )
                    return {
                        "success": False,
                        "error": (
                            f"Skill failed security scan "
                            f"(max severity: {scan_result['max_severity']}): "
                            f"{findings_summary}"
                        ),
                        "scan_result": scan_result,
                    }
            except ImportError:
                logger.warning(
                    f"clawcare not installed, skipping security scan for {parsed_skill.name}",
                )

            # Determine project ID for the skill
            skill_project_id = ctx.project_id if project_scoped else None

            # Store the skill
            skill = ctx.storage.create_skill(
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

            # Persist skill files if loaded
            if hasattr(parsed_skill, "loaded_files") and parsed_skill.loaded_files:
                from gobby.storage.skills import SkillFile

                skill_files = [
                    SkillFile(
                        id="",
                        skill_id=skill.id,
                        path=lf.path,
                        file_type=lf.file_type,
                        content=lf.content,
                        content_hash=lf.content_hash,
                        size_bytes=lf.size_bytes,
                    )
                    for lf in parsed_skill.loaded_files
                ]
                ctx.storage.set_skill_files(skill.id, skill_files)

            return {
                "success": True,
                "installed": True,
                "skill_id": skill.id,
                "skill_name": skill.name,
                "source_type": source_type,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
