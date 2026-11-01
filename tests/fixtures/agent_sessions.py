"""Synthetic installed definitions and sessions with explicit capability grants."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager


class AgentSessions:
    def __init__(self, db: HubDatabase, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.next_seq = 1700

    def add(
        self,
        definition: str,
        writes: Sequence[str] = (),
        *,
        capabilities: tuple[str, ...] = (),
        definition_fields: dict[str, Any] | None = None,
    ) -> str:
        definitions = AgentDefinitionManager(self.db)
        if definitions.get_by_name(definition, self.project_id) is None:
            definitions.create(
                definition,
                {
                    "name": definition,
                    "version": "1.0",
                    "prompts": {"agent": "Complete the assigned task."},
                    "workflows": {"rule_selectors": {"include": []}},
                    "task_attestation_writes": list(writes),
                    "capabilities": list(capabilities),
                    **(definition_fields or {}),
                },
                project_id=self.project_id,
            )
        session_id = str(uuid4())
        self.next_seq += 1
        self.db.execute(
            "INSERT INTO sessions "
            "(id, external_id, machine_id, source, project_id, title, status, agent_depth, seq_num) "
            "VALUES (%s, %s, %s, 'test', %s, %s, 'active', 0, %s)",
            (
                session_id,
                f"ext-{session_id}",
                require_machine_id(),
                self.project_id,
                definition,
                self.next_seq,
            ),
        )
        SessionVariableManager(self.db).set_variable(session_id, "_agent_type", definition)
        return session_id
