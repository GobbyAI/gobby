"""
Skill installation functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles installing, backing up, and routing skills across CLI integrations.
"""

import logging
import shutil
from pathlib import Path
from shutil import copy2
from typing import Any

from gobby.cli.utils import get_install_dir

logger = logging.getLogger(__name__)


def backup_gobby_skills(skills_dir: Path) -> dict[str, Any]:
    """Move gobby-prefixed skill directories to a backup location.

    This function is called during installation to preserve existing gobby skills
    before they are replaced by database-synced skills. User custom skills
    (non-gobby prefixed) are not touched.

    Args:
        skills_dir: Path to skills directory (e.g., .claude/skills)

    Returns:
        Dict with:
        - success: bool
        - backed_up: int - number of skills moved to backup
        - skipped: str (optional) - reason for skipping
    """
    result: dict[str, Any] = {
        "success": True,
        "backed_up": 0,
    }

    if not skills_dir.exists():
        result["skipped"] = "skills directory does not exist"
        return result

    # Find gobby-prefixed skill directories
    gobby_skills = [d for d in skills_dir.iterdir() if d.is_dir() and d.name.startswith("gobby-")]

    if not gobby_skills:
        return result

    # Create backup directory (sibling to skills/)
    backup_dir = skills_dir.parent / "skills.backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Move each gobby skill to backup
    for skill_dir in gobby_skills:
        target = backup_dir / skill_dir.name
        # If already exists in backup, remove it first (replace with newer)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(skill_dir), str(target))
        result["backed_up"] += 1

    return result


def install_router_skills_as_commands(target_commands_dir: Path) -> list[str]:
    """Install router skills as flattened Claude commands.

    Claude Code uses .claude/commands/name.md format for slash commands.
    This function copies the gobby router skills from shared/skills/ to
    commands/ as flattened .md files.

    Also cleans up stale command files from removed skills (e.g., g.md).

    Args:
        target_commands_dir: Path to commands directory (e.g., .claude/commands)

    Returns:
        List of installed command names
    """
    shared_skills_dir = get_install_dir() / "shared" / "skills"
    installed: list[str] = []

    # Router skills to install as commands
    router_skills = ["gobby"]

    # Clean up stale command files from removed skills
    stale_commands = ["g.md"]
    for stale in stale_commands:
        stale_path = target_commands_dir / stale
        if stale_path.exists():
            try:
                stale_path.unlink()
                logger.info(f"Removed stale command: {stale}")
            except OSError as e:
                logger.warning(f"Failed to remove stale command {stale}: {e}")

    target_commands_dir.mkdir(parents=True, exist_ok=True)

    for skill_name in router_skills:
        source_skill_md = shared_skills_dir / skill_name / "SKILL.md"
        if not source_skill_md.exists():
            logger.warning(f"Router skill not found: {source_skill_md}")
            continue

        # Flatten: copy SKILL.md to commands/name.md
        target_cmd = target_commands_dir / f"{skill_name}.md"

        try:
            copy2(source_skill_md, target_cmd)
            installed.append(f"{skill_name}.md")
        except OSError as e:
            logger.error(f"Failed to copy router skill {skill_name}: {e}")

    return installed


def install_router_skills_as_cli_skills(target_skills_dir: Path) -> list[str]:
    """Install router skills using the ``skills/name/SKILL.md`` directory structure.

    This function copies the gobby router skills from shared/skills/ to the
    target skills directory preserving the directory structure.

    Also cleans up stale skill directories from removed skills (e.g., g/).

    Args:
        target_skills_dir: Path to skills directory (e.g., .qwen/skills)

    Returns:
        List of installed skill names
    """
    shared_skills_dir = get_install_dir() / "shared" / "skills"
    installed: list[str] = []

    # Router skills to install
    router_skills = ["gobby"]

    # Clean up stale skill directories from removed skills
    stale_skills = ["g"]
    for stale in stale_skills:
        stale_path = target_skills_dir / stale
        if stale_path.exists():
            try:
                shutil.rmtree(stale_path)
                logger.info(f"Removed stale skill directory: {stale}/")
            except OSError as e:
                logger.warning(f"Failed to remove stale skill {stale}: {e}")

    target_skills_dir.mkdir(parents=True, exist_ok=True)

    for skill_name in router_skills:
        source_skill_dir = shared_skills_dir / skill_name
        source_skill_md = source_skill_dir / "SKILL.md"
        if not source_skill_md.exists():
            logger.warning(f"Router skill not found: {source_skill_md}")
            continue

        # Create skill directory and copy SKILL.md
        target_skill_dir = target_skills_dir / skill_name
        target_skill_dir.mkdir(parents=True, exist_ok=True)
        target_skill_md = target_skill_dir / "SKILL.md"

        try:
            copy2(source_skill_md, target_skill_md)
            installed.append(f"{skill_name}/")
        except OSError as e:
            logger.error(f"Failed to copy router skill {skill_name}: {e}")

    return installed
