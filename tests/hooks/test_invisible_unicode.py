"""Native tools retain their output and expose hidden-carrier counts."""

import logging

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from tests.hooks._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("key", ["tool_output", "tool_response", "tool_result"])
def test_native_tool_warning_preserves_output(
    event_handlers: EventHandlers, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    output = {"text": "private payload\U000e0061\u200b\u202e\x00"}
    event = make_event(HookEventType.AFTER_TOOL, data={"tool_name": "WebFetch", key: output})
    with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
        response = event_handlers.handle_after_tool(event)
    assert response.decision == "allow"
    assert response.context is not None
    assert "tags=1, zero_width=1, bidi=1, controls=1" in response.context
    assert "original output is unchanged" in response.context
    assert "private payload" not in response.context
    assert "private payload" not in caplog.text
    assert event.data[key] is output
    assert output["text"] == "private payload\U000e0061\u200b\u202e\x00"


@pytest.mark.parametrize(
    "tool,output", [("WebFetch", "clean"), ("mcp__gobby__call_tool", "\u202e")]
)
def test_clean_native_and_mcp_outputs_do_not_warn(
    event_handlers: EventHandlers, tool: str, output: str
) -> None:
    event = make_event(HookEventType.AFTER_TOOL, data={"tool_name": tool, "tool_response": output})
    response = event_handlers.handle_after_tool(event)
    assert response.context is None
    assert response.modified_input is None
