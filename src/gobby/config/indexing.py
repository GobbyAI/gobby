"""Shared indexing configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IndexingConfig(BaseModel):
    """Indexing behavior shared by gcode and gwiki."""

    model_config = ConfigDict(extra="forbid")

    respect_gitignore: bool = Field(
        default=True,
        description=(
            "Respect .gitignore, .git/info/exclude, and global git excludes while indexing."
        ),
    )
