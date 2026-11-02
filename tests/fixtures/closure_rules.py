"""Inline policies for testing the generic closure engine and ancestor walk."""

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase


def install_closure_rules(db: HubDatabase) -> None:
    policies = {
        "fixture-reserved-parent": (
            "facts.get('closure_intent') == 'ancestor' and "
            "(task.get('claimed_by_session_id') or task.get('is_escalated') or "
            "(task.get('state') or {}).get('hold_state') or task.get('labels') or "
            "(task.get('task_type') != 'epic' and task.get('validation_criteria')))"
        ),
        "fixture-open-children": "not facts.get('children_closed', True)",
        "fixture-implementation": (
            "reason == 'done' and latest_attestation('implementation').get('result') != 'approved'"
        ),
    }
    manager = RuleDefinitionManager(db)
    for name, when in policies.items():
        manager.create(
            name=name,
            definition_json={
                "event": "task_close",
                "when": when,
                "effects": [{"type": "block", "reason": name}],
            },
        )
