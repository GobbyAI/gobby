"""Write-time classifier for rule delivery dispositions."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.workflows.delivery_disposition import apply_rule_delivery_dispositions

pytestmark = pytest.mark.unit


def _one_shot_definition(*, guard: str = "shown") -> dict[str, Any]:
    return {
        "event": "turn_start",
        "when": f"not variables.get('{guard}')",
        "effects": [
            {"type": "inject_context", "template": "hello"},
            {"type": "set_variable", "variable": guard, "value": True},
        ],
    }


def _delivery(effect: dict[str, Any]) -> str:
    value = effect.get("delivery", "eager")
    assert value in {"eager", "on_receipt"}
    return str(value)


class TestApplyRuleDeliveryDispositions:
    def test_one_shot_payload_and_guard_are_on_receipt(self) -> None:
        outcome = apply_rule_delivery_dispositions("once", _one_shot_definition())

        assert outcome.errors == ()
        assert outcome.changed is True
        assert [_delivery(effect) for effect in outcome.definition["effects"]] == [
            "on_receipt",
            "on_receipt",
        ]

    def test_block_acknowledge_is_on_receipt(self) -> None:
        outcome = apply_rule_delivery_dispositions(
            "guard-writes",
            {
                "event": "before_tool",
                "when": "not variables.get('nudge_fired')",
                "effects": [
                    {
                        "type": "block",
                        "reason": "use the plan artifact",
                        "acknowledge_variable": "nudge_fired",
                    }
                ],
            },
        )

        assert outcome.errors == ()
        assert outcome.changed is True
        assert _delivery(outcome.definition["effects"][0]) == "on_receipt"

    def test_mcp_success_variable_groups_sibling_payload(self) -> None:
        outcome = apply_rule_delivery_dispositions(
            "discover-hubs",
            {
                "event": "turn_start",
                "when": "not variables.get('skill_discovery_instructions_shown')",
                "effects": [
                    {"type": "load_skill", "skill": "loading-skills"},
                    {
                        "type": "mcp_call",
                        "server": "gobby-skills",
                        "tool": "list_hubs",
                        "inject_result": True,
                        "success_variable": "skill_discovery_instructions_shown",
                    },
                ],
            },
        )

        assert outcome.errors == ()
        assert [_delivery(effect) for effect in outcome.definition["effects"]] == [
            "on_receipt",
            "on_receipt",
        ]

    def test_mixed_repair_set_variable_stays_eager(self) -> None:
        outcome = apply_rule_delivery_dispositions(
            "check-memory",
            {
                "event": "turn_end",
                "when": "not variables.get('_memory_initial_stop_checked')",
                "effects": [
                    {
                        "type": "set_variable",
                        "variable": "_memory_initial_stop_checked",
                        "value": True,
                        "when": "skill_loaded('memory')",
                    },
                    {
                        "type": "block",
                        "reason": "load memory",
                        "acknowledge_variable": "_memory_initial_stop_checked",
                        "when": "not skill_loaded('memory')",
                    },
                ],
            },
        )

        assert outcome.errors == ()
        assert _delivery(outcome.definition["effects"][0]) == "eager"
        assert _delivery(outcome.definition["effects"][1]) == "on_receipt"
        assert "delivery" not in outcome.definition["effects"][0]

    def test_unguarded_payload_without_sibling_suppressor_stays_eager(self) -> None:
        outcome = apply_rule_delivery_dispositions(
            "load-memory-guidance-on-initial-turn",
            {
                "event": "turn_start",
                "when": "not variables.get('_memory_initial_stop_checked')",
                "effects": [{"type": "load_skill", "skill": "memory"}],
            },
        )

        assert outcome.errors == ()
        assert outcome.changed is False
        assert _delivery(outcome.definition["effects"][0]) == "eager"

    def test_ordinary_state_set_variable_stays_eager(self) -> None:
        outcome = apply_rule_delivery_dispositions(
            "queue-reviews",
            {
                "event": "after_tool",
                "effects": [
                    {
                        "type": "set_variable",
                        "variable": "_memory_pending_task_reviews",
                        "value": [],
                    }
                ],
            },
        )

        assert outcome.errors == ()
        assert outcome.changed is False

    def test_already_classified_is_unchanged(self) -> None:
        body = _one_shot_definition()
        for effect in body["effects"]:
            effect["delivery"] = "on_receipt"

        outcome = apply_rule_delivery_dispositions("once", body)

        assert outcome.errors == ()
        assert outcome.changed is False

    def test_explicit_eager_one_shot_is_rewritten(self) -> None:
        body = _one_shot_definition()
        for effect in body["effects"]:
            effect["delivery"] = "eager"

        outcome = apply_rule_delivery_dispositions("once", body)

        assert outcome.errors == ()
        assert outcome.changed is True
        assert [_delivery(effect) for effect in outcome.definition["effects"]] == [
            "on_receipt",
            "on_receipt",
        ]

    def test_ambiguous_suppressor_is_an_actionable_diagnostic(self) -> None:
        outcome = apply_rule_delivery_dispositions(
            "maybe-once",
            {
                "event": "turn_start",
                "when": "not variables.get('guard')",
                "effects": [
                    {"type": "inject_context", "template": "hello"},
                    {
                        "type": "set_variable",
                        "variable": "guard",
                        "value": True,
                        "when": "variables.get('other')",
                    },
                ],
            },
        )

        assert outcome.changed is False
        assert outcome.errors
        diagnostic = outcome.errors[0]
        assert "maybe-once" in diagnostic
        assert "set_variable" in diagnostic
        assert "guard" in diagnostic
