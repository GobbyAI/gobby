"""Shared models and errors for skill loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitHubRef:
    """Parsed GitHub repository reference."""

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
    """A file loaded from a skill directory with content and metadata."""

    path: str
    file_type: str
    content: str
    content_hash: str
    size_bytes: int


class SkillLoadError(Exception):
    """Error loading a skill from the filesystem."""

    def __init__(self, message: str, path: str | Path | None = None):
        self.path = str(path) if path else None
        super().__init__(f"{message}" + (f": {path}" if path else ""))
