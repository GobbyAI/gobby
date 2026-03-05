"""Test run dataclass for the gobby-tests system."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class TestRun:
    """A single test/lint/typecheck execution."""

    id: str
    category: str
    command: str
    status: Literal["running", "completed", "failed", "timeout"]
    started_at: str
    created_at: str
    session_id: str | None = None
    project_id: str | None = None
    exit_code: int | None = None
    summary: str | None = None
    output_file: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TestRun:
        """Convert database row to TestRun object."""
        keys = set(row.keys())
        return cls(
            id=row["id"],
            category=row["category"],
            command=row["command"],
            status=row["status"],
            started_at=row["started_at"],
            created_at=row["created_at"],
            session_id=row["session_id"] if "session_id" in keys else None,
            project_id=row["project_id"] if "project_id" in keys else None,
            exit_code=row["exit_code"] if "exit_code" in keys else None,
            summary=row["summary"] if "summary" in keys else None,
            output_file=row["output_file"] if "output_file" in keys else None,
            completed_at=row["completed_at"] if "completed_at" in keys else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert TestRun to full dictionary."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "category": self.category,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "output_file": self.output_file,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
        }

    def to_brief(self) -> dict[str, Any]:
        """Convert TestRun to brief format for tool responses."""
        return {
            "run_id": self.id,
            "category": self.category,
            "status": self.status,
            "exit_code": self.exit_code,
            "summary": self.summary,
        }
