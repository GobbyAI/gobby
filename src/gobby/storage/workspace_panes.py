"""Workspace pane rows and their public serialization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


@dataclass
class WorkspacePane:
    """One workspace_panes row; terminal_id is NULL while its spawn is in flight."""

    id: str
    tab_id: str
    ref: int
    terminal_id: str | None
    owns_terminal: bool
    label: str | None
    created_at: datetime
    updated_at: datetime
    role: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkspacePane:
        return cls(
            id=str(row["id"]),
            tab_id=str(row["tab_id"]),
            ref=int(row["ref"]),
            terminal_id=_optional_str(row["terminal_id"]),
            owns_terminal=bool(row["owns_terminal"]),
            label=_optional_str(row["label"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            role=_optional_str(row["role"]),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.role is None:
            result.pop("role")
        return result
