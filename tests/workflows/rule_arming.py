"""Install retry-limit rules for engine tests.

The engine only counts repeated denials of one tool; a rule marked
``retry_limit`` decides when that count escalates. Behavior tests install a
synthetic one. Contract tests arm every bundled retry-limit rule, found by its
field, so no test depends on a bundled rule's name.
"""

from __future__ import annotations

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent

SYNTHETIC_RETRY_LIMIT_RULE = "fixture-retry-limit"
SYNTHETIC_ESCALATION_PREFIX = "Escalated after repeated denials"

RETRY_LIMIT_WHEN = (
    "block_state.get('_last_blocked_tool')"
    " and block_state.get('consecutive_tool_blocks', 0) > 0"
    " and block_state.get('consecutive_tool_blocks', 0) + 1"
    " >= variables.get('max_consecutive_blocked_tool_attempts', 5)"
)


def install_retry_limit_rule(db: HubDatabase) -> None:
    """Install a synthetic retry-limit rule that quotes the original block reason."""
    RuleDefinitionManager(db).create(
        name=SYNTHETIC_RETRY_LIMIT_RULE,
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            mandatory=True,
            retry_limit=True,
            when=RETRY_LIMIT_WHEN,
            effects=[
                RuleEffect(
                    type="block",
                    reason=(
                        SYNTHETIC_ESCALATION_PREFIX
                        + ": {{ block_state.get('_last_blocked_tool') }}"
                        " x{{ block_state.get('consecutive_tool_blocks', 0) + 1 }}"
                        " | {{ block_state.get('_last_blocked_reason') or 'none' }}"
                    ),
                )
            ],
        ).model_dump_json(),
        enabled=True,
        priority=0,
    )
