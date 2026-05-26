"""Prompt synchronization for bundled prompts.

This module provides sync_bundled_prompts() which loads prompts from the
bundled install/shared/prompts/ directory and syncs them to the database.
"""

import logging
from pathlib import Path
from typing import Any

from gobby.prompts.models import parse_frontmatter
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.prompts import LocalPromptManager

__all__ = ["get_bundled_prompts_path", "sync_bundled_prompts"]

logger = logging.getLogger(__name__)


def get_bundled_prompts_path() -> Path:
    """Get the path to bundled prompts directory.

    Returns:
        Path to src/gobby/install/shared/prompts/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "prompts"


def sync_bundled_prompts(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled prompts from install/shared/prompts/ to the database.

    This function:
    1. Walks all .md files in the bundled prompts directory
    2. Parses frontmatter + body from each file
    3. Creates new records or updates changed content (idempotent)
    4. All records are created with scope='bundled' and project_id=None

    Args:
        db: Database connection

    Returns:
        Dict with success status and counts
    """
    prompts_path = get_bundled_prompts_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not prompts_path.exists():
        logger.warning(f"Bundled prompts path not found: {prompts_path}")
        result["errors"].append(f"Prompts path not found: {prompts_path}")
        return result

    # dev_mode=True so we can update bundled records during sync
    manager = LocalPromptManager(db, dev_mode=True)

    for md_file in sorted(prompts_path.rglob("*.md")):
        try:
            raw_content = md_file.read_text(encoding="utf-8")
            frontmatter, body = parse_frontmatter(raw_content)

            # Derive prompt name from relative path (without .md extension)
            rel_path = md_file.relative_to(prompts_path)
            name = str(rel_path.with_suffix(""))

            description = frontmatter.get("description", "")
            version = str(frontmatter.get("version", "1.0"))
            source_path = str(md_file)

            # Extract variables from frontmatter (multiple formats supported)
            variables = _extract_variables(frontmatter)

            # Check if prompt already exists (bundled scope)
            existing = manager.get_bundled(name)

            if existing is not None:
                # Compare key fields to detect stale content
                needs_update = (
                    existing.description != description
                    or existing.content != body.strip()
                    or existing.version != version
                    or existing.variables != variables
                    or existing.source_path != source_path
                )

                if needs_update:
                    manager.update_prompt(
                        prompt_id=existing.id,
                        description=description,
                        content=body.strip(),
                        version=version,
                        variables=variables,
                        source_path=source_path,
                    )
                    logger.info(f"Updated bundled prompt: {name}")
                    result["updated"] += 1
                else:
                    logger.debug(f"Prompt '{name}' already up to date, skipping")
                    result["skipped"] += 1
                continue

            # Create the prompt in the database
            manager.create_prompt(
                name=name,
                description=description,
                content=body.strip(),
                version=version,
                variables=variables,
                scope="bundled",
                source_path=source_path,
                project_id=None,
            )
            logger.info(f"Synced bundled prompt: {name}")
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync prompt '{md_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        f"Prompt sync complete: {result['synced']} synced, "
        f"{result['updated']} updated, {result['skipped']} skipped, {total} total"
    )

    return result


def _extract_variables(frontmatter: dict[str, Any]) -> dict[str, Any] | None:
    """Extract variable specifications from frontmatter.

    Supports multiple frontmatter formats:
    - ``variables:`` dict with type/default/description/required specs
    - ``required_variables:`` / ``optional_variables:`` lists
    - ``defaults:`` dict for default values

    Returns:
        Dict of variable specs suitable for JSON storage, or None.
    """
    variables: dict[str, Any] = {}

    # Standard variables dict (e.g., expansion prompts)
    if "variables" in frontmatter and isinstance(frontmatter["variables"], dict):
        variables = frontmatter["variables"]

    # required_variables / optional_variables lists (e.g., memory prompts)
    if "required_variables" in frontmatter:
        for var_name in frontmatter["required_variables"]:
            if var_name not in variables:
                variables[var_name] = {"type": "str", "required": True}

    if "optional_variables" in frontmatter:
        for var_name in frontmatter["optional_variables"]:
            if var_name not in variables:
                variables[var_name] = {"type": "str", "required": False}

    # defaults dict
    if "defaults" in frontmatter and isinstance(frontmatter["defaults"], dict):
        for var_name, default_val in frontmatter["defaults"].items():
            if var_name in variables and isinstance(variables[var_name], dict):
                variables[var_name]["default"] = default_val
            else:
                variables[var_name] = {"type": "str", "default": default_val}

    return variables if variables else None
