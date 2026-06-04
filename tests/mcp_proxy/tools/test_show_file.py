"""Unit tests for the show_file MCP tool."""

from __future__ import annotations

import pytest

from gobby.mcp_proxy.tools.artifacts import (
    MAX_IMAGE_FILE_SIZE,
    MAX_TEXT_FILE_SIZE,
    create_artifacts_registry,
    set_artifact_broadcaster,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_state():
    """Reset artifact broadcaster state before and after each test."""
    set_artifact_broadcaster(None)
    yield
    set_artifact_broadcaster(None)


class MockBroadcaster:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, **kwargs: object) -> None:
        self.events.append(kwargs)


@pytest.fixture
def artifact_bc():
    bc = MockBroadcaster()
    set_artifact_broadcaster(bc)
    return bc


@pytest.fixture
def registry():
    return create_artifacts_registry()


async def test_show_file_python(registry, artifact_bc, tmp_path):
    """Python files should produce type=code, language=python."""
    f = tmp_path / "app.py"
    f.write_text("print('hello')\n", encoding="utf-8")

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is True
    assert result["type"] == "code"
    assert result["language"] == "python"
    assert result["title"] == "app.py"

    assert len(artifact_bc.events) == 1
    ev = artifact_bc.events[0]
    assert ev["event"] == "show_file"
    assert ev["artifact_type"] == "code"
    assert ev["content"] == "print('hello')\n"
    assert ev["language"] == "python"


async def test_show_file_markdown(registry, artifact_bc, tmp_path):
    """Markdown files should produce type=text, language=markdown."""
    f = tmp_path / "README.md"
    f.write_text("# Hello\n\nWorld\n", encoding="utf-8")

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is True
    assert result["type"] == "text"
    assert result["language"] == "markdown"
    assert artifact_bc.events[0]["content"] == "# Hello\n\nWorld\n"


async def test_show_file_image_base64(registry, artifact_bc, tmp_path):
    """Image files should be returned as base64 data URIs."""
    f = tmp_path / "icon.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is True
    assert result["type"] == "image"
    content = artifact_bc.events[0]["content"]
    assert content.startswith("data:image/png;base64,")


async def test_show_file_csv(registry, artifact_bc, tmp_path):
    """CSV files should produce type=sheet."""
    f = tmp_path / "data.csv"
    f.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is True
    assert result["type"] == "sheet"
    assert result["language"] is None


async def test_show_file_not_found(registry, artifact_bc):
    """Missing files should return an error."""
    tool = registry.get_tool("show_file")
    result = await tool(file_path="/nonexistent/file.py", conversation_id="conv_1")

    assert result["success"] is False
    assert "not found" in result["error"].lower()
    assert len(artifact_bc.events) == 0


async def test_show_file_relative_path_rejected(registry, artifact_bc):
    """Relative paths should be rejected."""
    tool = registry.get_tool("show_file")
    result = await tool(file_path="relative/path.py", conversation_id="conv_1")

    assert result["success"] is False
    assert "absolute" in result["error"].lower()


async def test_show_file_too_large(registry, artifact_bc, tmp_path):
    """Files exceeding size limit should be rejected."""
    f = tmp_path / "big.py"
    f.write_bytes(b"x" * (MAX_TEXT_FILE_SIZE + 1))

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is False
    assert "too large" in result["error"].lower()
    assert len(artifact_bc.events) == 0


async def test_show_file_image_too_large(registry, artifact_bc, tmp_path):
    """Image files exceeding image size limit should be rejected."""
    f = tmp_path / "huge.png"
    f.write_bytes(b"\x89PNG" + b"\x00" * (MAX_IMAGE_FILE_SIZE + 1))

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is False
    assert "too large" in result["error"].lower()


async def test_show_file_unknown_extension(registry, artifact_bc, tmp_path):
    """Unknown extensions should default to code with extension as language."""
    f = tmp_path / "script.zig"
    f.write_text('const std = @import("std");\n', encoding="utf-8")

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), conversation_id="conv_1")

    assert result["success"] is True
    assert result["type"] == "code"
    assert result["language"] == "zig"


async def test_show_file_custom_title(registry, artifact_bc, tmp_path):
    """Custom title should override the filename."""
    f = tmp_path / "main.py"
    f.write_text("pass\n", encoding="utf-8")

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f), title="Entry Point", conversation_id="conv_1")

    assert result["title"] == "Entry Point"
    assert artifact_bc.events[0]["title"] == "Entry Point"


async def test_show_file_no_conversation_id(registry, artifact_bc, tmp_path):
    """Missing conversation_id should return an error."""
    f = tmp_path / "test.py"
    f.write_text("pass\n", encoding="utf-8")

    tool = registry.get_tool("show_file")
    result = await tool(file_path=str(f))

    assert result["success"] is False
    assert "conversation_id" in result["error"]
