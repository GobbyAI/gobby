"""
Skill installation functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles installing and routing skills across CLI integrations.
"""

import hashlib
import logging
from pathlib import Path

from gobby.cli.utils import get_install_dir
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.capability_routing import capability_menu

logger = logging.getLogger(__name__)


def _router_carrier(source: Path) -> str:
    """Project catalog metadata into provider carriers without reference bodies."""
    catalog = load_capability_catalog(source.parent)
    return (
        source.read_text(encoding="utf-8")
        + "\n## Available Capabilities\n\n"
        + "Generated from the bundled catalog. Use the provider's active trigger.\n\n"
        + capability_menu(catalog, "<trigger>")
        + "\n"
    )


# Exact historical bundled bytes identify ownership; names and prefixes do not.
_BUNDLED_GOBBY_HASHES = frozenset(
    {
        "619531082d1317fbc0b42e8b13ba92df4ae4ee21e88e786b12c9cf72039b7f4e",  # dc1a751129
        "12b5b4402ff2dc8b90bd1d3d6502b24587c9729e96472f046d2fc036985b4bc1",  # ec5536f254
        "774726c1b641648e4736e3bd0e706a32fdf025e1398e9ff8e4aedd907b8fedd2",  # ec5536f254 rendered
        "b9b1e66283cf5a0d80dec69c580a6151a7241398c9d021c7820604f7a57451d6",  # e0eed4ddaf
        "cb7f478cbdbd5d0ecbca64099be84d23a3b529cc9e4ebb1d8deae57298402ddb",  # 0d51a0fcf0
        "77d1a124df137210684f4485ae82cfef8ec7618fe1101fbedd6c869e3ecdda39",  # 444fd544c3
        "5a552473646a34f65106a34d8607d7957a41c5e542d6441b7f3bfd4b8e5ee71f",  # acf2634373
        "baf3f276119ad229e99ba24d7116dce2d06e1b16dee08fd7dd553744a53a0182",  # 12ef592236
        "579092110277d5b07fdbe48c8bbdb4aba78a89c3cf6ca767e7144c5cf423da46",  # 15e6e6d054
        "8b830aef032edcc0023833d282e5951ea021c3761873077e1520f24e5dafe6a5",  # 8aed18b656
        "8b3e05b86b3ba2652b29c585f09080ede30097d0d0507740b2bd7f67c9686f93",  # 9edf8aa3d0
        "dcf3fc877ade2ca50a346cfe08614582ff18b6022024214ccce99214254fe7a4",  # 62f7b64abf
        "0f5e97772a1caf406e59b30d89fba8abb3e0cb230bd9fc1af502b3d5e6d45b2a",  # bd9d42aaf7
        "dd94b473c8ab314a63b63ecdb40a6131f06cfe23d8d1ba0f8cee3c9149863de9",  # e3a657d9c1
        "3807630475f54b0459706ae2335676296f3c01e710aa8226739d55cefeb21363",  # dfd745572d
        "5e1a28b2ac5325ae7e4077ec8a2a24bcde825f56f70cb860ec1ce154521f189c",  # 15b0fbbf59
        "67d62e20cb2bc0c16e491245e1d1fa05225175dd7cb4e6f761f1a74bfbe843e3",  # f7cfb46e9a
        "2b96a30a24bab8c625a28fd4afb2df0fc94b7def950bd74f952a80580214ecb8",  # 46915c8863
    }
)

_BUNDLED_G_HASHES = frozenset(
    {
        "611e07170a162809dc8b25e94a3e4d8f2318d2cf003d6e9844b06b3ce9595d17",  # f7cfb46e9a
        "245025ba7dbb9b44dbccf26d579fb385e0eac824e92788a0c1e6e23668b001d4",  # 7624b4a3a6
    }
)


def _preserve_custom(path: Path) -> None:
    logger.warning("Preserved custom carrier %s; move it aside to install the bundled router", path)


def _write_router(target: Path, source: Path) -> bool:
    """Only replace absent files or byte-verified Gobby carriers."""
    content = _router_carrier(source)
    if target.is_symlink():
        _preserve_custom(target)
        return False
    if target.exists():
        if not target.is_file():
            _preserve_custom(target)
            return False
        existing = target.read_bytes()
        if existing == content.encode("utf-8"):
            return True
        if (
            existing != source.read_bytes()
            and hashlib.sha256(existing).hexdigest() not in _BUNDLED_GOBBY_HASHES
        ):
            _preserve_custom(target)
            return False
    target.write_text(content, encoding="utf-8")
    return True


def _retire_short_alias(path: Path) -> bool:
    """Remove only verified bundled aliases, preserving symlinks and extra files."""
    if path.is_symlink():
        _preserve_custom(path)
        return False
    if not path.exists():
        return False
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() not in _BUNDLED_G_HASHES:
        _preserve_custom(path)
        return False
    path.unlink()
    return True


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

    target_commands_dir.mkdir(parents=True, exist_ok=True)

    for skill_name in router_skills:
        source_skill_md = shared_skills_dir / skill_name / "SKILL.md"
        if not source_skill_md.exists():
            logger.warning("Router skill not found: %s", source_skill_md)
            continue

        # Flatten: copy SKILL.md to commands/name.md
        target_cmd = target_commands_dir / f"{skill_name}.md"

        try:
            if _write_router(target_cmd, source_skill_md):
                installed.append(f"{skill_name}.md")
                _retire_short_alias(target_commands_dir / "g.md")
        except OSError as e:
            logger.error("Failed to copy router skill %s: %s", skill_name, e)

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

    target_skills_dir.mkdir(parents=True, exist_ok=True)

    for skill_name in router_skills:
        source_skill_dir = shared_skills_dir / skill_name
        source_skill_md = source_skill_dir / "SKILL.md"
        if not source_skill_md.exists():
            logger.warning("Router skill not found: %s", source_skill_md)
            continue

        # Create skill directory and copy SKILL.md
        target_skill_dir = target_skills_dir / skill_name
        if target_skill_dir.is_symlink() or (
            target_skill_dir.exists() and not target_skill_dir.is_dir()
        ):
            _preserve_custom(target_skill_dir)
            continue
        target_skill_dir.mkdir(parents=True, exist_ok=True)
        target_skill_md = target_skill_dir / "SKILL.md"

        try:
            if _write_router(target_skill_md, source_skill_md):
                installed.append(f"{skill_name}/")
                stale_dir = target_skills_dir / "g"
                if stale_dir.is_symlink():
                    _preserve_custom(stale_dir)
                elif stale_dir.is_dir():
                    retired = _retire_short_alias(stale_dir / "SKILL.md")
                    if retired and not any(stale_dir.iterdir()):
                        stale_dir.rmdir()
        except OSError as e:
            logger.error("Failed to copy router skill %s: %s", skill_name, e)

    return installed
