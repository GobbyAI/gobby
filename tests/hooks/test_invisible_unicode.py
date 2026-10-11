"""Native tools retain their output and expose hidden-carrier counts."""

import logging

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from tests.hooks._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("key", ["tool_output", "tool_response", "tool_result"])
@pytest.mark.parametrize("tool", ["WebFetch", "mcp__claude-in-chrome__get_page_text"])
def test_native_tool_warning_preserves_output(
    event_handlers: EventHandlers, key: str, caplog: pytest.LogCaptureFixture, tool: str
) -> None:
    output = {"text": "private payload\U000e0061\u200b\u202e\x00"}
    event = make_event(HookEventType.AFTER_TOOL, data={"tool_name": tool, key: output})
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


@pytest.mark.parametrize(
    "tool,output",
    [("Read", "\ufefffile content"), ("Bash", "\x1b[32mPASSED\x1b[0m")],
)
def test_display_annotations_do_not_warn(
    event_handlers: EventHandlers, tool: str, output: str
) -> None:
    event = make_event(HookEventType.AFTER_TOOL, data={"tool_name": tool, "tool_output": output})
    response = event_handlers.handle_after_tool(event)
    assert response.context is None
    assert event.data["tool_output"] == output


@pytest.mark.parametrize(
    "tool,output,count",
    [
        ("Read", "\ufefftext\ufeff", "zero_width=1"),
        ("Bash", "\x1b[32mtext\x00\x1b[0m", "controls=1"),
    ],
)
def test_display_annotations_do_not_hide_other_carriers(
    event_handlers: EventHandlers, tool: str, output: str, count: str
) -> None:
    event = make_event(HookEventType.AFTER_TOOL, data={"tool_name": tool, "tool_output": output})
    response = event_handlers.handle_after_tool(event)
    assert response.context is not None
    assert count in response.context
    assert event.data["tool_output"] == output
