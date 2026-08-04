from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from gobby.servers.routes.llm import ChatCompletionsPayload

pytestmark = pytest.mark.unit


def _payload(speed_mode: str) -> dict[str, object]:
    return {
        "messages": [{"role": "user", "content": "hello"}],
        "project_path": "/repo",
        "tool_policy": {"cli": "gcode", "tools": ["search"]},
        "caller": "test",
        "request_id": str(uuid4()),
        "speed_mode": speed_mode,
    }


def test_chat_completions_rejects_bad_speed_mode() -> None:
    assert ChatCompletionsPayload.model_validate(_payload("fast")).speed_mode == "fast"
    assert ChatCompletionsPayload.model_validate(_payload("standard")).speed_mode == "standard"

    with pytest.raises(ValidationError):
        ChatCompletionsPayload.model_validate(_payload("turbo"))
