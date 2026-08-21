"""Claude SDK image mapping for text generation."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.llm.claude import ClaudeLLMProvider

pytestmark = pytest.mark.unit


class MockAssistantMessage:
    def __init__(self, content: list[object]) -> None:
        self.content = content


class MockResultMessage:
    def __init__(self, result: str | None = None, usage: dict[str, Any] | None = None) -> None:
        self.result = result
        self.usage = usage


class MockTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class MockToolUseBlock:
    def __init__(self, id: str, name: str, input: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.input = input


class MockClaudeAgentOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs: dict[str, Any] = kwargs
        self.settings: str | None = None
        self.setting_sources: list[str] | None = None
        self.stderr: Any = None


@pytest.fixture
def claude_config() -> DaemonConfig:
    return DaemonConfig()


@contextmanager
def mock_claude_sdk(
    mock_query_func: Callable[..., AsyncIterator[object]],
) -> Iterator[None]:
    async def query_wrapper(*args: object, **kwargs: object) -> AsyncIterator[object]:
        prompt = kwargs.get("prompt", args[0] if args else None)
        options = kwargs.get("options", args[1] if len(args) > 1 else None)
        async for message in mock_query_func(prompt, options):
            yield message

    with (
        patch("gobby.llm.claude_cli.shutil.which", return_value="/mock/claude"),
        patch("gobby.llm.claude_cli._is_usable_cli_path", return_value=True),
        patch("gobby.llm.claude_cli.os.path.exists", return_value=True),
        patch("gobby.llm.claude_cli.os.access", return_value=True),
        patch("gobby.llm.claude_sdk.query", query_wrapper),
        patch("gobby.llm.claude_sdk.AssistantMessage", MockAssistantMessage),
        patch("gobby.llm.claude_sdk.ResultMessage", MockResultMessage),
        patch("gobby.llm.claude_sdk.TextBlock", MockTextBlock),
        patch("gobby.llm.claude_sdk.ToolUseBlock", MockToolUseBlock),
        patch("gobby.llm.claude_sdk.ClaudeAgentOptions", MockClaudeAgentOptions),
    ):
        yield


@pytest.mark.asyncio
async def test_generate_text_with_images_renders_blocks(
    claude_config: DaemonConfig,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "scene.png"
    image_bytes = b"\x89PNG\r\n" + b"\x00" * 24
    image_path.write_bytes(image_bytes)
    encoded = base64.standard_b64encode(image_bytes).decode("utf-8")
    captured: dict[str, object] = {}

    async def mock_query(prompt: object, _options: object) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        if hasattr(prompt, "__aiter__"):
            captured["messages"] = [message async for message in prompt]
        yield MockAssistantMessage([MockTextBlock("a red cube")])
        yield MockResultMessage("a red cube")

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        result = await provider.generate_text_result(
            "caption this",
            images=[str(image_path)],
        )

    assert result.text == "a red cube"
    messages = captured.get("messages")
    assert isinstance(messages, list)
    assert messages
    content = messages[0]["message"]["content"]
    assert {"type": "text", "text": "caption this"} in content
    image_blocks = [block for block in content if block.get("type") == "image"]
    assert image_blocks == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": encoded,
            },
        }
    ]
