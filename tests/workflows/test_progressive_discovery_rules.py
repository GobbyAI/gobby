"""Generic rule tool prefilters."""

import pytest

from gobby.workflows.definitions import RuleDefinitionBody

pytestmark = pytest.mark.unit


class TestRuleDefinitionBodyToolsField:
    """Verify tools field on RuleDefinitionBody works as pre-filter."""

    def test_tools_field_accepted(self) -> None:
        """RuleDefinitionBody should accept a tools field."""
        from gobby.workflows.definitions import RuleEffect, RuleTriggerEvent

        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            tools=["mcp__gobby__list_tools"],
            effects=[RuleEffect(type="block", reason="test")],
        )
        assert body.tools == ["mcp__gobby__list_tools"]

    def test_tools_field_none_by_default(self) -> None:
        """RuleDefinitionBody.tools should default to None."""
        from gobby.workflows.definitions import RuleEffect, RuleTriggerEvent

        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
        )
        assert body.tools is None
