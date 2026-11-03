"""Rule-selected variable targets share one trust boundary."""

from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.core import RuleEngine
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.unit

TARGETS = [
    ("variable", {"type": "set_variable", "value": True}),
    (
        "success_variable",
        {"type": "mcp_call", "server": "synthetic", "tool": "read", "inject_result": True},
    ),
    ("acknowledge_variable", {"type": "block", "reason": "Synthetic gate"}),
]


@pytest.mark.parametrize(("target_field", "effect"), TARGETS)
@pytest.mark.parametrize("delivery", ["eager", "on_receipt"])
@pytest.mark.parametrize(
    ("source", "tags", "variable", "allowed"),
    [
        ("custom", [], "_agent_type", False),
        ("custom", [], "_handoff_turn_end_pending", False),
        ("custom", [], "enforce_synthetic", False),
        ("custom", [], "project_policy", False),
        ("custom", [], "ordinary_flag", True),
        ("installed", ["gobby"], "_agent_type", True),
        ("installed", ["user", "gobby"], "_agent_type", False),
        ("installed", [], "project_policy", True),
    ],
)
@pytest.mark.asyncio
async def test_rule_selected_variable_write_authority(
    temp_db: HubDatabase,
    target_field: str,
    effect: dict[str, Any],
    delivery: str,
    source: Literal["installed", "custom", "project"],
    tags: list[str],
    variable: str,
    allowed: bool,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    manager.create(
        name="synthetic-writer",
        source=source,
        tags=["default", *tags],
        definition_json={
            "event": "before_tool",
            "effects": [{**effect, target_field: variable, "delivery": delivery}],
        },
    )

    async def dispatch(*args: Any) -> dict[str, Any]:
        return {"success": True}

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatch)
    # The sentinel doubles as the seat name when the case writes _agent_type; it must resolve.
    seed_seat(temp_db, "unchanged", include=["*"])
    variables: dict[str, Any] = {variable: "unchanged"}
    response = await engine.evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="11111111-1111-4111-8111-111111111111",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Read", "tool_input": {"file_path": "example.py"}},
        ),
        session_id="11111111-1111-4111-8111-111111111111",
        variables=variables,
    )

    assert variables[variable] == (True if allowed else "unchanged")
    staged = response.metadata.get(STAGED_EFFECTS_FIELD, {}).get("session_variables", {})
    if allowed and delivery == "on_receipt":
        assert staged[variable] is True
    else:
        assert variable not in staged
    assert response.decision == ("block" if target_field == "acknowledge_variable" else "allow")


@pytest.mark.parametrize(("target_field", "effect"), TARGETS)
def test_writer_model_enumerates_active_target(target_field: str, effect: dict[str, Any]) -> None:
    raw = {**effect, target_field: "synthetic_target"}
    assert RuleEffect.variable_write_targets(raw) == ["synthetic_target"]
    # Every target-shaped model field must carry writer metadata. Adding a new
    # target requires declaring its owning effect on the model, not in an adapter.
    for name, field in RuleEffect.model_fields.items():
        if name == "variable" or name.endswith("_variable"):
            assert isinstance(field.json_schema_extra, dict)
            assert field.json_schema_extra.get("variable_writer_for")


@pytest.mark.parametrize("delivery", ["eager", "on_receipt"])
@pytest.mark.parametrize(
    ("source", "tags", "variable", "allowed"),
    [
        ("custom", [], "_handoff_turn_end_pending", False),
        ("custom", [], "ordinary_flag", True),
        ("installed", ["gobby"], "_agent_type", True),
        ("installed", ["gobby", "user"], "_agent_type", False),
        ("installed", [], "project_policy", True),
    ],
)
def test_cached_and_late_injection_success_uses_rule_authority(
    temp_db: HubDatabase,
    delivery: Literal["eager", "on_receipt"],
    source: Literal["installed", "custom", "project"],
    tags: list[str],
    variable: str,
    allowed: bool,
) -> None:
    row = RuleDefinitionManager(temp_db).create(
        name="synthetic-cache-writer",
        source=source,
        tags=tags,
        definition_json={
            "event": "turn_start",
            "effects": [{"type": "inject_context", "template": "Synthetic"}],
        },
    )
    effect = RuleEffect(
        type="mcp_call",
        server="synthetic",
        tool="read",
        inject_result=True,
        success_variable=variable,
        delivery=delivery,
    )
    variables: dict[str, Any] = {variable: "unchanged"}
    staged: dict[str, Any] = {}

    RuleEngine(temp_db)._record_injection_success(effect, row, variables, staged)

    assert variables[variable] == (True if allowed else "unchanged")
    assert staged == ({variable: True} if allowed and delivery == "on_receipt" else {})
