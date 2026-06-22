"""Filesystem scanning helpers for skill loading."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from gobby.skills._loader_models import LoadedSkillFile

logger = logging.getLogger(__name__)

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
    """Classify a file by its location in the skill directory."""
    if filename in _LICENSE_FILES:
        return "license"

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
    """Check if a file is likely binary."""
    if file_path.suffix.lower() in _BINARY_EXTENSIONS:
        return True

    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except OSError:
        return True


def scan_subdirectory(skill_dir: Path, subdir_name: str) -> list[str] | None:
    """Scan a skill subdirectory and return relative file paths."""
    subdir = skill_dir / subdir_name
    if not subdir.exists() or not subdir.is_dir():
        return None

    try:
        skill_dir_resolved = skill_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        return None

    files: list[str] = []
    for file_path in subdir.rglob("*"):
        if file_path.is_symlink():
            continue

        if file_path.is_file():
            try:
                resolved = file_path.resolve()
                resolved.relative_to(skill_dir_resolved)
            except (OSError, RuntimeError, ValueError):
                continue

            rel_path = file_path.relative_to(skill_dir)
            files.append(str(rel_path))

    return sorted(files) if files else None


def load_skill_files(skill_dir: Path) -> list[LoadedSkillFile]:
    """Recursively load all non-binary files from a skill directory."""
    try:
        skill_dir_resolved = skill_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        return []
    files: list[LoadedSkillFile] = []

    for file_path in skill_dir.rglob("*"):
        if not file_path.is_file():
            continue

        if file_path.is_symlink():
            continue

        if any(part.startswith(".") for part in file_path.relative_to(skill_dir).parts):
            continue

        rel_parts = file_path.relative_to(skill_dir).parts
        if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue

        if file_path.name in _SKIP_FILES:
            continue

        try:
            resolved = file_path.resolve()
            resolved.relative_to(skill_dir_resolved)
        except (OSError, RuntimeError, ValueError):
            continue

        if _is_binary_file(file_path):
            continue

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
