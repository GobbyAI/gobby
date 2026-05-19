"""Agent command storage for parent-child session coordination.

Manages commands sent from parent sessions to child agent sessions,
tracking lifecycle from pending through running to completion.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.storage.hub.protocol import Row

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase
    from gobby.storage.hub.protocol import HubDatabase


@dataclass
class AgentCommand:
    """A command sent from a parent session to a child agent session."""

    id: str
    from_session: str
    to_session: str
    command_text: str
    status: str
    created_at: str
    allowed_tools: str | None = None
    allowed_mcp_tools: str | None = None
    exit_condition: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> AgentCommand:
        """Create instance from database row."""
        return cls(
            id=row["id"],
            from_session=row["from_session"],
            to_session=row["to_session"],
            command_text=row["command_text"],
            allowed_tools=row["allowed_tools"],
            allowed_mcp_tools=row["allowed_mcp_tools"],
            exit_condition=row["exit_condition"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "command_text": self.command_text,
            "allowed_tools": self.allowed_tools,
            "allowed_mcp_tools": self.allowed_mcp_tools,
            "exit_condition": self.exit_condition,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class AgentCommandManager:
    """Manages agent commands in the database."""

    def __init__(self, db: LocalDatabase | HubDatabase) -> None:
        self.db = db

    def create_command(
        self,
        from_session: str,
        to_session: str,
        command_text: str,
        allowed_tools: list[str] | None = None,
        allowed_mcp_tools: list[str] | None = None,
        exit_condition: str | None = None,
    ) -> AgentCommand:
        """Create a new agent command."""
        command_id = str(uuid.uuid4())
        tools_json = json.dumps(allowed_tools) if allowed_tools else None
        mcp_tools_json = json.dumps(allowed_mcp_tools) if allowed_mcp_tools else None

        self.db.execute(
            """INSERT INTO agent_commands
               (id, from_session, to_session, command_text,
                allowed_tools, allowed_mcp_tools, exit_condition, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                command_id,
                from_session,
                to_session,
                command_text,
                tools_json,
                mcp_tools_json,
                exit_condition,
            ),
        )

        row = self.db.fetchone("SELECT * FROM agent_commands WHERE id = ?", (command_id,))
        assert row is not None
        return AgentCommand.from_row(row)

    def get_command(self, command_id: str) -> AgentCommand | None:
        """Get a command by ID."""
        row = self.db.fetchone("SELECT * FROM agent_commands WHERE id = ?", (command_id,))
        return AgentCommand.from_row(row) if row else None

    def list_commands(
        self,
        to_session: str,
        status: str | None = None,
    ) -> list[AgentCommand]:
        """List commands for a target session."""
        if status:
            rows = self.db.fetchall(
                "SELECT * FROM agent_commands WHERE to_session = ? AND status = ? ORDER BY created_at",
                (to_session, status),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM agent_commands WHERE to_session = ? ORDER BY created_at",
                (to_session,),
            )
        return [AgentCommand.from_row(row) for row in rows]

    def update_status(self, command_id: str, status: str) -> AgentCommand:
        """Update command status with appropriate timestamp."""
        now = datetime.now(UTC).isoformat()

        if status == "running":
            self.db.execute(
                "UPDATE agent_commands SET status = ?, started_at = ? WHERE id = ?",
                (status, now, command_id),
            )
        elif status in ("completed", "failed", "cancelled"):
            self.db.execute(
                "UPDATE agent_commands SET status = ?, completed_at = ? WHERE id = ?",
                (status, now, command_id),
            )
        else:
            self.db.execute(
                "UPDATE agent_commands SET status = ? WHERE id = ?",
                (status, command_id),
            )

        cmd = self.get_command(command_id)
        if cmd is None:
            raise ValueError(f"Command '{command_id}' not found")
        return cmd
