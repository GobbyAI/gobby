"""SkillLoader - Load skills from filesystem, GitHub, and ZIP archives.

This module provides the SkillLoader class for loading skills from:
- Single SKILL.md files
- Directories containing SKILL.md files
- Recursively from a root directory
- GitHub repositories
- ZIP archives
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess  # nosec B404 # required for git clone/pull operations with validated input
import tempfile
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from gobby.skills.parser import ParsedSkill, SkillParseError, parse_skill_file
from gobby.skills.validator import SkillValidator
from gobby.storage.skills import SkillSourceType

logger = logging.getLogger(__name__)

# Default cache directory for cloned GitHub repos
DEFAULT_CACHE_DIR = Path.home() / ".gobby" / "skill-cache"


@dataclass
class GitHubRef:
    """Parsed GitHub repository reference.

    Attributes:
        owner: Repository owner (user or org)
        repo: Repository name
        branch: Branch or tag name (None for default branch)
        path: Path within the repository to skill directory
    """

    owner: str
    repo: str
    branch: str | None = None
    path: str | None = None

    @property
    def clone_url(self) -> str:
        """Get the HTTPS clone URL."""
        return f"https://github.com/{self.owner}/{self.repo}.git"

    @property
    def cache_key(self) -> str:
        """Get a unique key for caching this repo/branch combo."""
        branch_part = self.branch or "HEAD"
        return f"{self.owner}/{self.repo}/{branch_part}"


@dataclass
class LoadedSkillFile:
    """A file loaded from a skill directory with content and metadata.

    Attributes:
        path: Relative path from skill root (e.g. "references/api.md" or "forms.md")
        file_type: Classification: "script", "reference", "asset", "license", "resource"
        content: File text content
        content_hash: SHA-256 hex digest of content
        size_bytes: File size in bytes
    """

    path: str
    file_type: str
    content: str
    content_hash: str
    size_bytes: int


# File extensions considered binary (skip these during loading)
_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".webp",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".webm",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".class",
        ".jar",
        ".o",
        ".a",
    }
)

# Directories to skip during recursive scan
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".svn",
        ".hg",
        ".DS_Store",
        "__MACOSX",
    }
)

# Files to skip (SKILL.md is handled separately as main content)
_SKIP_FILES = frozenset({"SKILL.md"})

# License file names (stored as type "license", not surfaced to agents)
_LICENSE_FILES = frozenset(
    {"LICENSE", "LICENSE.txt", "LICENSE.md", "LICENCE", "LICENCE.txt", "LICENCE.md"}
)


def _classify_file(rel_path: str, filename: str) -> str:
    """Classify a file by its location in the skill directory.

    Args:
        rel_path: Relative path from skill root (e.g. "scripts/build.sh")
        filename: Just the filename (e.g. "build.sh")

    Returns:
        File type: "script", "reference", "asset", "license", or "resource"
    """
    if filename in _LICENSE_FILES:
        return "license"

    # Classify by parent directory
    parts = rel_path.split("/")
    if len(parts) > 1:
        top_dir = parts[0].lower()
        if top_dir == "scripts":
            return "script"
        if top_dir in ("references", "reference"):
            return "reference"
        if top_dir == "assets":
            return "asset"

    return "resource"


def _is_binary_file(file_path: Path) -> bool:
    """Check if a file is likely binary.

    Uses extension check first, then null-byte detection in first 8KB.

    Args:
        file_path: Path to the file

    Returns:
        True if file appears to be binary
    """
    if file_path.suffix.lower() in _BINARY_EXTENSIONS:
        return True

    # Check for null bytes in first 8KB
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except OSError:
        return True  # Can't read = skip


def _resolve_within_directory(base: Path, relative_path: str) -> Path:
    resolved_base = base.resolve()
    resolved = (resolved_base / relative_path).resolve()
    try:
        resolved.relative_to(resolved_base)
    except ValueError:
        raise SkillLoadError("Path would escape extracted ZIP", base) from None
    return resolved


def parse_github_url(url: str) -> GitHubRef:
    """Parse a GitHub URL into its components.

    Supports formats:
    - owner/repo
    - owner/repo#branch
    - github:owner/repo
    - github:owner/repo#branch
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo/tree/branch
    - https://github.com/owner/repo/tree/branch/path/to/skill

    Args:
        url: GitHub URL in any supported format

    Returns:
        GitHubRef with parsed components

    Raises:
        ValueError: If URL cannot be parsed
    """
    if not url or not url.strip():
        raise ValueError("Invalid GitHub URL: empty string")

    url = url.strip()

    # Format: github:owner/repo#branch
    if url.startswith("github:"):
        url = url[7:]  # Remove "github:" prefix
        return _parse_owner_repo_format(url)

    # Format: https://github.com/owner/repo...
    if url.startswith("https://github.com/") or url.startswith("http://github.com/"):
        return _parse_full_github_url(url)

    # Format: owner/repo#branch
    if "/" in url and not url.startswith("http"):
        return _parse_owner_repo_format(url)

    raise ValueError(f"Invalid GitHub URL: {url}")


def _parse_owner_repo_format(url: str) -> GitHubRef:
    """Parse owner/repo#branch format."""
    branch = None

    # Check for branch suffix
    if "#" in url:
        url, branch = url.rsplit("#", 1)

    # Split owner/repo
    parts = url.split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")

    owner = parts[0]
    repo = parts[1].removesuffix(".git")

    return GitHubRef(owner=owner, repo=repo, branch=branch)


def _parse_full_github_url(url: str) -> GitHubRef:
    """Parse full https://github.com/... URL."""
    # Remove protocol
    url = re.sub(r"^https?://github\.com/", "", url)

    # Remove trailing slash
    url = url.rstrip("/")

    # Remove .git suffix
    url = url.removesuffix(".git")

    parts = url.split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")

    owner = parts[0]
    repo = parts[1]
    branch = None
    path = None

    # Check for /tree/branch/path format
    if len(parts) > 2 and parts[2] == "tree":
        if len(parts) > 3:
            branch = parts[3]
        if len(parts) > 4:
            path = "/".join(parts[4:])

    return GitHubRef(owner=owner, repo=repo, branch=branch, path=path)


def _validate_github_ref(ref: GitHubRef) -> None:
    """Validate GitHub reference components for safety.

    Args:
        ref: GitHubRef to validate

    Raises:
        SkillLoadError: If validation fails
    """
    # Safe characters for owner/repo: alphanumeric, hyphen, underscore, dot
    safe_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    # Safe branch name: alphanumeric, hyphen, underscore, dot, forward slash
    # No leading hyphen (could be interpreted as command flag)
    safe_branch_pattern = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./-]*$")

    # Validate owner
    if not ref.owner or len(ref.owner) > 100:
        raise SkillLoadError(f"Invalid GitHub owner: {ref.owner}")
    if not safe_name_pattern.match(ref.owner):
        raise SkillLoadError(f"Invalid characters in GitHub owner: {ref.owner}")

    # Validate repo
    if not ref.repo or len(ref.repo) > 100:
        raise SkillLoadError(f"Invalid GitHub repo: {ref.repo}")
    if not safe_name_pattern.match(ref.repo):
        raise SkillLoadError(f"Invalid characters in GitHub repo: {ref.repo}")

    # Validate branch if present
    if ref.branch:
        if len(ref.branch) > 200:
            raise SkillLoadError(f"Branch name too long: {ref.branch}")
        if not safe_branch_pattern.match(ref.branch):
            raise SkillLoadError(f"Invalid characters in branch name: {ref.branch}")
        # Reject shell metacharacters and path traversal
        if ".." in ref.branch or any(
            c in ref.branch for c in ("$", "`", ";", "&", "|", "<", ">", "\\", "\n", "\r")
        ):
            raise SkillLoadError(f"Invalid branch name: {ref.branch}")


def clone_skill_repo(
    ref: GitHubRef,
    cache_dir: Path | None = None,
) -> Path:
    """Clone or update a GitHub repository.

    Args:
        ref: Parsed GitHub reference
        cache_dir: Directory to cache cloned repos (default: ~/.gobby/skill-cache)

    Returns:
        Path to the cloned repository

    Raises:
        SkillLoadError: If clone/pull fails or validation fails
    """
    # Validate input before any filesystem or subprocess operations
    _validate_github_ref(ref)

    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    repo_path = cache_dir / ref.owner / ref.repo
    is_existing = repo_path.exists() and (repo_path / ".git").exists()

    if is_existing:
        # Update existing repo
        if ref.branch:
            # Checkout the specific branch first (git command with validated ref)
            checkout_cmd = ["git", "-C", str(repo_path), "checkout", ref.branch]
            result = subprocess.run(  # nosec B603 # hardcoded git command, input validated
                checkout_cmd, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                raise SkillLoadError(
                    f"Failed to checkout branch {ref.branch}: {result.stderr}",
                    ref.clone_url,
                )
        # Pull latest changes (hardcoded git command)
        pull_cmd = ["git", "-C", str(repo_path), "pull", "--ff-only"]
        result = subprocess.run(  # nosec B603 # hardcoded git command
            pull_cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise SkillLoadError(
                f"Failed to pull repository updates: {result.stderr}",
                ref.clone_url,
            )
        return repo_path
    else:
        # Clone new repo
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1"]
        if ref.branch:
            cmd.extend(["--branch", ref.branch])
        cmd.extend([ref.clone_url, str(repo_path)])

        result = subprocess.run(  # nosec B603 # hardcoded git clone, input validated
            cmd, capture_output=True, text=True, timeout=120
        )

        if result.returncode != 0:
            raise SkillLoadError(
                f"Failed to clone repository: {result.stderr}",
                ref.clone_url,
            )

        return repo_path


@contextmanager
def extract_zip(zip_path: str | Path) -> Generator[Path]:
    """Extract a ZIP archive to a temporary directory.

    This context manager extracts the contents of a ZIP file to a temporary
    directory, yields the path to the extracted contents, and cleans up
    the temporary directory on exit (even if an exception occurs).

    Args:
        zip_path: Path to the ZIP file

    Yields:
        Path to the temporary directory containing extracted contents

    Raises:
        SkillLoadError: If ZIP file not found or invalid

    Example:
        ```python
        with extract_zip("skills.zip") as temp_path:
            skill = loader.load_skill(temp_path / "my-skill")
        # temp_path is automatically deleted here
        ```
    """
    zip_path = Path(zip_path)

    if not zip_path.exists():
        raise SkillLoadError("ZIP file not found", zip_path)

    if not zipfile.is_zipfile(zip_path):
        raise SkillLoadError("Invalid ZIP file", zip_path)

    temp_dir = tempfile.mkdtemp(prefix="gobby-skill-")
    temp_path = Path(temp_dir)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Safe extraction to prevent zip-slip attacks
            for member in zf.infolist():
                # Build and normalize the target path
                target_path = (temp_path / member.filename).resolve()

                # Verify the target is inside temp_path (prevent zip-slip)
                try:
                    target_path.relative_to(temp_path.resolve())
                except ValueError:
                    raise SkillLoadError(
                        f"Zip entry would extract outside target: {member.filename}",
                        zip_path,
                    ) from None

                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                else:
                    # Ensure parent directory exists
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    # Extract file content
                    with zf.open(member) as source, open(target_path, "wb") as dest:
                        shutil.copyfileobj(source, dest)

        yield temp_path
    finally:
        # Clean up temp directory
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)


class SkillLoadError(Exception):
    """Error loading a skill from the filesystem."""

    def __init__(self, message: str, path: str | Path | None = None):
        self.path = str(path) if path else None
        super().__init__(f"{message}" + (f": {path}" if path else ""))


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
            skill.loaded_files = self._load_skill_files(path)

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
        subdir = skill_dir / subdir_name
        if not subdir.exists() or not subdir.is_dir():
            return None

        # Resolve skill_dir once for security checks
        skill_dir_resolved = skill_dir.resolve()

        files: list[str] = []
        for file_path in subdir.rglob("*"):
            # Skip symlinks to prevent traversal attacks
            if file_path.is_symlink():
                continue

            if file_path.is_file():
                # Verify resolved path is within skill directory
                try:
                    resolved = file_path.resolve()
                    # Check that resolved path is under skill_dir
                    resolved.relative_to(skill_dir_resolved)
                except (OSError, ValueError):
                    # Skip files that can't be resolved or are outside skill_dir
                    continue

                # Get path relative to skill directory
                rel_path = file_path.relative_to(skill_dir)
                files.append(str(rel_path))

        return sorted(files) if files else None

    def _load_skill_files(self, skill_dir: Path) -> list[LoadedSkillFile]:
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
        skill_dir_resolved = skill_dir.resolve()
        files: list[LoadedSkillFile] = []

        for file_path in skill_dir.rglob("*"):
            if not file_path.is_file():
                continue

            # Skip symlinks
            if file_path.is_symlink():
                continue

            # Skip dotfiles
            if any(part.startswith(".") for part in file_path.relative_to(skill_dir).parts):
                continue

            # Skip excluded directories
            rel_parts = file_path.relative_to(skill_dir).parts
            if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
                continue

            # Skip SKILL.md and other excluded files
            if file_path.name in _SKIP_FILES:
                continue

            # Security: verify resolved path stays within skill directory
            try:
                resolved = file_path.resolve()
                resolved.relative_to(skill_dir_resolved)
            except (OSError, ValueError):
                continue

            # Skip binary files
            if _is_binary_file(file_path):
                continue

            # Read content
            rel_path = str(file_path.relative_to(skill_dir))
            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                logger.debug(f"Skipping unreadable file: {rel_path}")
                continue

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            file_type = _classify_file(rel_path, file_path.name)

            files.append(
                LoadedSkillFile(
                    path=rel_path,
                    file_type=file_type,
                    content=content,
                    content_hash=content_hash,
                    size_bytes=len(content.encode("utf-8")),
                )
            )

        return sorted(files, key=lambda f: f.path)

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
            except SkillLoadError as e:
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
        if ref.path:
            skill_path = repo_path / ref.path
        else:
            skill_path = repo_path

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
