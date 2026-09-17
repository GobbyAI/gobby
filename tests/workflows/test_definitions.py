"""Tests for WorkflowDefinition models in definitions.py."""

import pytest

pytestmark = pytest.mark.unit


# --- WorkflowDefinition tests ---


def test_workflow_definition_defaults() -> None:
    """Test that WorkflowDefinition has correct defaults for new fields."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(name="test-wf")

    assert wf.enabled is True
    assert wf.priority == 100
    assert wf.session_variables == {}


def test_workflow_definition_with_new_fields() -> None:
    """Test that WorkflowDefinition accepts enabled, priority, session_variables."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(
        name="custom-wf",
        enabled=False,
        priority=25,
        session_variables={"task_claimed": "false", "stop_attempts": "0"},
    )

    assert wf.enabled is False
    assert wf.priority == 25
    assert wf.session_variables == {"task_claimed": "false", "stop_attempts": "0"}


def test_workflow_definition_type_lifecycle_compat() -> None:
    """Test that YAML with type: lifecycle still parses without error."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(name="session-lifecycle", type="lifecycle")
    assert wf.type == "lifecycle"


def test_workflow_definition_type_step_compat() -> None:
    """Test that YAML with type: step still parses without error."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(name="auto-task", type="step")
    assert wf.type == "step"


def test_workflow_definition_type_default() -> None:
    """Test that type defaults to 'step' when not specified."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(name="test-wf")
    assert wf.type == "step"


def test_workflow_definition_all_new_fields_with_type() -> None:
    """Test that new fields coexist with existing type field."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(
        name="full-wf",
        type="lifecycle",
        enabled=True,
        priority=10,
        session_variables={"unlocked_tools": "[]"},
        variables={"internal_var": "default"},
    )

    assert wf.type == "lifecycle"
    assert wf.enabled is True
    assert wf.priority == 10
    assert wf.session_variables == {"unlocked_tools": "[]"}
    assert wf.variables == {"internal_var": "default"}


def test_workflow_definition_session_variables_vs_variables() -> None:
    """Test that session_variables and variables are independent."""
    from gobby.workflows.definitions import WorkflowDefinition

    wf = WorkflowDefinition(
        name="test-wf",
        variables={"workflow_scoped": True},
        session_variables={"session_shared": True},
    )

    assert wf.variables == {"workflow_scoped": True}
    assert wf.session_variables == {"session_shared": True}


def test_rule_definition_omits_unsupplied_metadata_defaults() -> None:
    from gobby.workflows.definitions import split_rule_definition_data

    body, metadata = split_rule_definition_data(
        {
            "event": "before_tool",
            "effects": [{"type": "block", "reason": "blocked"}],
        }
    )

    assert body["event"] == "before_tool"
    assert metadata == {}


def test_rule_definition_preserves_explicit_metadata() -> None:
    from gobby.workflows.definitions import split_rule_definition_data

    _, metadata = split_rule_definition_data(
        {
            "event": "before_tool",
            "effects": [{"type": "block", "reason": "blocked"}],
            "enabled": True,
            "priority": 100,
            "tags": [],
        }
    )

    assert metadata == {"enabled": True, "priority": 100, "tags": []}


def test_rule_definition_metadata_rejects_invalid_values() -> None:
    from pydantic import ValidationError

    from gobby.workflows.definitions import split_rule_definition_data

    with pytest.raises(ValidationError):
        split_rule_definition_data(
            {
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "blocked"}],
                "enabled": "sometimes",
            }
        )


def test_set_variable_effect_accepts_command_selectors() -> None:
    """A set_variable effect carries command selectors; the engine honors them."""
    import warnings
    from datetime import UTC, datetime

    from gobby.hooks.events import HookEvent, HookEventType, SessionSource
    from gobby.workflows.definitions import RuleEffect
    from gobby.workflows.engine.effects import EffectsMixin

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        effect = RuleEffect(
            type="set_variable",
            variable="code_review_fresh",
            value=True,
            command_pattern=r"git\s+commit",
        )

    assert [str(record.message) for record in caught] == []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        RuleEffect(type="set_variable", variable="v", value=1, reason="belongs to block")

    assert [str(record.message) for record in caught] == [
        "RuleEffect(type='set_variable') has 'reason' set "
        "(relevant to 'block' effects, ignored here)"
    ]

    def event_for(command: str) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Bash", "tool_input": {"command": command}},
        )

    matcher = EffectsMixin()
    assert matcher._effect_matches_event(effect, event_for("git commit -m x"))
    assert not matcher._effect_matches_event(effect, event_for("git status"))
