"""Rule rows whose bodies predate create-time validation."""

import json
from typing import Any

from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase


def store_unvalidated_rule(
    db: HubDatabase, name: str, definition: dict[str, Any], *, enabled: bool
) -> RuleDefinitionRow:
    """Store a body that create() now refuses, as a row written before validation would be."""
    manager = RuleDefinitionManager(db)
    placeholder = {"event": "before_tool", "effects": [{"type": "observe"}]}
    row = manager.create(name, placeholder, source="custom", enabled=enabled)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE rule_definitions SET definition_json = %s WHERE id = %s",
            (json.dumps(definition), row.id),
        )
    stored = manager.get(row.id)
    assert stored.definition_json == definition
    return stored
