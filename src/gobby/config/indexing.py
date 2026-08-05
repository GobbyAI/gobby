"""Shared indexing configuration."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_ExcludePattern = Annotated[
    str,
    StringConstraints(min_length=1, pattern=r"^[^/\\]+$"),
]


class IndexingConfig(BaseModel):
    """Indexing behavior shared by gcode and gwiki."""

    model_config = ConfigDict(extra="forbid")

    respect_gitignore: bool = Field(
        default=True,
        description=(
            "Respect .gitignore, .git/info/exclude, and global git excludes while indexing."
        ),
    )
    extra_excludes: list[_ExcludePattern] = Field(
        default_factory=list,
        description=(
            "Additional component-name glob patterns to exclude alongside built-in patterns."
        ),
    )
