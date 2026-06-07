"""Shared indexing configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IndexingConfig(BaseModel):
    """Indexing behavior shared by gcode and gwiki."""

    respect_gitignore: bool = Field(
        default=True,
        description=(
            "Respect .gitignore, .git/info/exclude, and global git excludes while indexing."
        ),
    )
