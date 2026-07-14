"""SkillLoader - Load skills from filesystem, GitHub, and ZIP archives.

This module provides the SkillLoader class for loading skills from:
- Single SKILL.md files
- Directories containing SKILL.md files
- Recursively from a root directory
- GitHub repositories
- ZIP archives
"""

from __future__ import annotations

import logging
from pathlib import Path

from gobby.skills._loader_files import _classify_file, load_skill_files, scan_subdirectory
from gobby.skills._loader_github import (
    DEFAULT_CACHE_DIR,
    clone_skill_repo,
    parse_github_url,
    resolve_github_skill_path,
)
from gobby.skills._loader_models import GitHubRef, LoadedSkillFile, SkillLoadError
from gobby.skills._loader_zip import _resolve_within_directory, extract_zip
from gobby.skills.parser import ParsedSkill, SkillParseError, parse_skill_file
from gobby.skills.validator import SkillValidator
from gobby.storage.skills import SkillSourceType

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CACHE_DIR",
    "GitHubRef",
    "LoadedSkillFile",
    "SkillLoadError",
    "SkillLoader",
    "_classify_file",
    "clone_skill_repo",
    "extract_zip",
    "parse_github_url",
    "resolve_github_skill_path",
]


class SkillLoader:
    """Load skills from the filesystem.

    This class handles loading skills from:
    - Single SKILL.md files
    - Directories containing SKILL.md
    - Recursively from a skills root directory

    Example usage:
        ```python
        from gobby.skills.loader import SkillLoader

        loader = SkillLoader()

        # Load a single skill
        skill = loader.load_skill("path/to/SKILL.md")

        # Load from a skill directory
        skill = loader.load_skill("path/to/skill-name/")

        # Load all skills from a directory
        skills = loader.load_directory("path/to/skills/")
        ```
    """

    def __init__(
        self,
        default_source_type: SkillSourceType = "local",
    ):
        """Initialize the loader.

        Args:
            default_source_type: Default source type for loaded skills
        """
        self._default_source_type = default_source_type
        self._validator = SkillValidator()

    def load_skill(
        self,
        path: str | Path,
        validate: bool = True,
        check_dir_name: bool = True,
    ) -> ParsedSkill:
        """Load a skill from a file or directory.

        Args:
            path: Path to SKILL.md file or directory containing SKILL.md
            validate: Whether to validate the skill
            check_dir_name: Whether to check that directory name matches skill name

        Returns:
            ParsedSkill loaded from the path

        Raises:
            SkillLoadError: If skill cannot be loaded
        """
        path = Path(path)

        if not path.exists():
            raise SkillLoadError("Path not found", path)

        # Determine the actual SKILL.md path
        if path.is_file():
            skill_file = path
            is_directory_load = False
        else:
            skill_file = path / "SKILL.md"
            if not skill_file.exists():
                raise SkillLoadError("SKILL.md not found in directory", path)
            is_directory_load = True

        # Parse the skill file
        try:
            skill = parse_skill_file(skill_file)
        except SkillParseError as e:
            raise SkillLoadError(f"Failed to parse skill: {e}", skill_file) from e

        # Check directory name matches skill name (when loading from directory)
        if is_directory_load and check_dir_name:
            dir_name = path.name
            if skill.name != dir_name:
                raise SkillLoadError(
                    f"Directory name mismatch: directory '{dir_name}' "
                    f"does not match skill name '{skill.name}'",
                    path,
                )

        # Validate the skill
        if validate:
            result = self._validator.validate(skill)
            if not result.valid:
                errors = "; ".join(result.errors)
                raise SkillLoadError(
                    f"Skill validation failed: {errors}",
                    skill_file,
                )

        # Detect directory structure (scripts/, references/, assets/)
        if is_directory_load:
            skill.scripts = self._scan_subdirectory(path, "scripts")
            skill.references = self._scan_subdirectory(path, "references")
            skill.assets = self._scan_subdirectory(path, "assets")

            # Load all files with content for multi-file support
            skill.loaded_files = self._load_skill_files(
                path,
                initial_size_bytes=len(skill.content.encode("utf-8")),
            )

        # Set source tracking
        skill.source_path = str(skill_file)
        skill.source_type = self._default_source_type

        return skill

    def _scan_subdirectory(self, skill_dir: Path, subdir_name: str) -> list[str] | None:
        """Scan a subdirectory for files and return relative paths.

        Args:
            skill_dir: Path to the skill directory
            subdir_name: Name of the subdirectory (scripts, references, assets)

        Returns:
            List of relative file paths, or None if directory doesn't exist or is empty
        """
        return scan_subdirectory(skill_dir, subdir_name)

    def _load_skill_files(
        self,
        skill_dir: Path,
        initial_size_bytes: int = 0,
    ) -> list[LoadedSkillFile]:
        """Recursively scan a skill directory and load all non-binary files.

        Classifies files by location:
        - scripts/** → "script"
        - references/** or reference/** → "reference"
        - assets/** → "asset"
        - LICENSE/LICENSE.txt/LICENSE.md → "license"
        - Everything else → "resource"

        Skips SKILL.md (content stored in skills.content), binary files,
        dotfiles, __pycache__, node_modules.

        Args:
            skill_dir: Path to the skill directory

        Returns:
            List of LoadedSkillFile with content and hashes
        """
        try:
            return load_skill_files(skill_dir, initial_size_bytes=initial_size_bytes)
        except ValueError as e:
            raise SkillLoadError(str(e), skill_dir) from e

    def load_directory(
        self,
        path: str | Path,
        validate: bool = True,
    ) -> list[ParsedSkill]:
        """Load all skills from a directory.

        Scans for subdirectories containing SKILL.md files and loads them.
        Non-skill directories and files are ignored.

        Args:
            path: Path to directory containing skill subdirectories
            validate: Whether to validate loaded skills

        Returns:
            List of ParsedSkill objects

        Raises:
            SkillLoadError: If directory not found
        """
        path = Path(path)

        if not path.exists():
            raise SkillLoadError("Directory not found", path)

        if not path.is_dir():
            raise SkillLoadError("Path is not a directory", path)

        skills: list[ParsedSkill] = []

        for item in path.iterdir():
            if not item.is_dir():
                continue

            skill_file = item / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                skill = self.load_skill(item, validate=validate)
                skills.append(skill)
            except Exception as e:
                logger.warning(f"Skipping invalid skill: {e}")
                continue

        return skills

    def scan_skills(
        self,
        path: str | Path,
    ) -> list[Path]:
        """Scan a directory for skill directories.

        Finds all subdirectories containing SKILL.md without loading them.

        Args:
            path: Path to scan

        Returns:
            List of paths to skill directories
        """
        path = Path(path)

        if not path.exists() or not path.is_dir():
            return []

        skill_dirs: list[Path] = []

        for item in path.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                skill_dirs.append(item)

        return skill_dirs

    def load_from_github(
        self,
        url: str,
        validate: bool = True,
        load_all: bool = False,
        cache_dir: Path | None = None,
    ) -> ParsedSkill | list[ParsedSkill]:
        """Load skill(s) from a GitHub repository.

        Supports formats:
        - owner/repo - Single skill repo
        - owner/repo#branch - With specific branch
        - github:owner/repo - With github: prefix
        - https://github.com/owner/repo - Full URL
        - https://github.com/owner/repo/tree/branch/path - With path to skill

        Args:
            url: GitHub URL in any supported format
            validate: Whether to validate loaded skills
            load_all: If True, load all skills from repo (returns list)
            cache_dir: Optional cache directory override

        Returns:
            ParsedSkill if load_all=False, list[ParsedSkill] if load_all=True

        Raises:
            SkillLoadError: If skill cannot be loaded
        """
        ref = parse_github_url(url)
        repo_path = clone_skill_repo(ref, cache_dir=cache_dir)

        # Determine the skill path within the repo
        skill_path = resolve_github_skill_path(repo_path, ref.path)

        if load_all:
            # Load all skills from the repo
            skills = self.load_directory(skill_path, validate=validate)
            for skill in skills:
                skill.source_type = "github"
                skill.source_path = f"github:{ref.owner}/{ref.repo}"
                skill.source_ref = ref.branch
            return skills
        else:
            # Load single skill
            skill = self.load_skill(
                skill_path,
                validate=validate,
                check_dir_name=False,  # Don't check dir name for GitHub imports
            )
            skill.source_type = "github"
            skill.source_path = f"github:{ref.owner}/{ref.repo}"
            skill.source_ref = ref.branch
            return skill

    def load_from_zip(
        self,
        zip_path: str | Path,
        validate: bool = True,
        load_all: bool = False,
        internal_path: str | None = None,
    ) -> ParsedSkill | list[ParsedSkill]:
        """Load skill(s) from a ZIP archive.

        The ZIP can contain:
        - A single skill directory with SKILL.md
        - A SKILL.md at the root
        - Multiple skill directories (use load_all=True)

        Args:
            zip_path: Path to the ZIP file
            validate: Whether to validate loaded skills
            load_all: If True, load all skills from ZIP (returns list)
            internal_path: Path within the ZIP to the skill directory

        Returns:
            ParsedSkill if load_all=False, list[ParsedSkill] if load_all=True

        Raises:
            SkillLoadError: If skill cannot be loaded
        """
        zip_path = Path(zip_path)

        if not zip_path.exists():
            raise SkillLoadError("ZIP file not found", zip_path)

        with extract_zip(zip_path) as temp_path:
            # Determine the skill path within the extracted contents
            if internal_path:
                skill_path = _resolve_within_directory(temp_path, internal_path)
            else:
                # Check for SKILL.md at root
                if (temp_path / "SKILL.md").exists():
                    skill_path = temp_path
                else:
                    # Look for a skill directory
                    skill_dirs = self.scan_skills(temp_path)
                    if skill_dirs:
                        if load_all:
                            skill_path = temp_path
                        else:
                            skill_path = skill_dirs[0]
                    else:
                        # Try the temp path itself
                        skill_path = temp_path

            if load_all:
                # Load all skills from the ZIP
                skills = self.load_directory(skill_path, validate=validate)
                for skill in skills:
                    skill.source_type = "zip"
                    skill.source_path = f"zip:{zip_path}"
                return skills
            else:
                # Load single skill
                skill = self.load_skill(
                    skill_path,
                    validate=validate,
                    check_dir_name=False,  # Don't check dir name for ZIP imports
                )
                skill.source_type = "zip"
                skill.source_path = f"zip:{zip_path}"
                return skill
