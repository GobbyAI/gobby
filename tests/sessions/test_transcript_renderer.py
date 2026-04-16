from datetime import UTC, datetime

import pytest

from gobby.sessions.transcript_renderer import (
    RenderState,
    classify_tool,
    extract_result_metadata,
    render_incremental,
    render_transcript,
)
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit


def make_msg(
    index: int,
    role: str,
    content: str,
    content_type: str = "text",
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict | None = None,
    tool_result: str | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=content_type,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=tool_result,
        timestamp=datetime.now(UTC),
        raw_json={},
        tool_use_id=tool_use_id,
    )


def test_render_transcript_groups_assistant_text():
    msgs = [
        make_msg(0, "user", "hello"),
        make_msg(1, "assistant", "Hi "),
        make_msg(2, "assistant", "there!"),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 2
    assert rendered[0].role == "user"
    assert rendered[0].content == "hello"
    assert rendered[1].role == "assistant"
    assert rendered[1].content == "Hi there!"
    assert len(rendered[1].content_blocks) == 1
    assert rendered[1].content_blocks[0].content == "Hi there!"


def test_render_transcript_pairs_tool_results():
    msgs = [
        make_msg(
            0,
            "assistant",
            "",
            content_type="tool_use",
            tool_use_id="call-1",
            tool_name="mcp__test__tool",
            tool_input={"a": 1},
        ),
        make_msg(
            1,
            "user",
            "Result here",
            content_type="tool_result",
            tool_use_id="call-1",
            tool_result={"out": "ok"},
        ),
    ]
    rendered = render_transcript(msgs)
    # Both should be in ONE assistant message because tool_result is paired
    assert len(rendered) == 1
    assert rendered[0].role == "assistant"
    assert len(rendered[0].content_blocks) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls[0].id == "call-1"
    assert block.tool_calls[0].result is not None
    assert block.tool_calls[0].result.content == {"out": "ok"}


def test_render_transcript_deduplicates_streaming_content():
    msgs = [
        make_msg(0, "assistant", "Hello"),
        make_msg(1, "assistant", "Hello"),  # Duplicate
        make_msg(2, "assistant", " World"),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == "Hello World"


def test_render_transcript_classifies_hook_feedback_as_system():
    msgs = [
        make_msg(0, "user", "Stop hook feedback: something happened"),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].role == "system"


def test_render_transcript_strips_hook_context_from_user():
    msgs = [
        make_msg(0, "user", "hello <hook_context>some metadata</hook_context>"),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == "hello"


def test_render_transcript_strips_proposed_plan_tags():
    msgs = [
        make_msg(0, "assistant", "<proposed_plan>My detailed plan here</proposed_plan>"),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == "My detailed plan here"


def test_render_transcript_strips_proposed_implementation_tags():
    msgs = [
        make_msg(
            0,
            "assistant",
            "<proposed_implementation>Code implementation</proposed_implementation>",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == "Code implementation"


def test_render_transcript_strips_search_quality_reflection_tags():
    msgs = [
        make_msg(
            0,
            "assistant",
            "Result: <search_quality_reflection>thinking...</search_quality_reflection> done",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == "Result: thinking... done"


def test_render_transcript_renders_permissions_instructions_as_protocol_tool():
    msgs = [
        make_msg(
            0,
            "system",
            "<permissions instructions>Filesystem sandboxing defines which files can be read or written.</permissions instructions>",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == ""
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls is not None
    assert block.tool_calls[0].tool_type == "protocol"
    assert block.tool_calls[0].tool_name == "protocol_context"
    assert block.tool_calls[0].arguments == {"tag": "permissions instructions"}
    assert block.tool_calls[0].result is not None
    assert (
        block.tool_calls[0].result.content
        == "Filesystem sandboxing defines which files can be read or written."
    )


def test_render_transcript_renders_collaboration_mode_as_protocol_tool():
    msgs = [
        make_msg(
            0,
            "system",
            "<collaboration_mode>Collaboration Mode: Default</collaboration_mode>",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == ""
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls is not None
    assert block.tool_calls[0].arguments == {"tag": "collaboration_mode"}
    assert block.tool_calls[0].result is not None
    assert block.tool_calls[0].result.content == "Collaboration Mode: Default"


def test_render_transcript_renders_turn_aborted_as_protocol_tool():
    msgs = [
        make_msg(
            0,
            "system",
            "<turn_aborted>The user interrupted the previous turn.</turn_aborted>",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == ""
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls is not None
    assert block.tool_calls[0].arguments == {"tag": "turn_aborted"}
    assert block.tool_calls[0].result is not None
    assert block.tool_calls[0].result.content == "The user interrupted the previous turn."


def test_render_transcript_interleaves_environment_context_protocol_tool():
    msgs = [
        make_msg(
            0,
            "system",
            "Visible\n<environment_context><shell>zsh</shell></environment_context>",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == "Visible"
    assert len(rendered[0].content_blocks) == 2
    assert rendered[0].content_blocks[0].type == "text"
    protocol_block = rendered[0].content_blocks[1]
    assert protocol_block.type == "tool_chain"
    assert protocol_block.tool_calls is not None
    assert protocol_block.tool_calls[0].arguments == {"tag": "environment_context"}
    assert protocol_block.tool_calls[0].result is not None
    assert protocol_block.tool_calls[0].result.content == {"shell": "zsh"}


def test_render_transcript_renders_instruction_wrappers_as_protocol_tools_case_insensitively():
    msgs = [
        make_msg(
            0,
            "system",
            "<INSTRUCTIONS># Repository Guidelines</INSTRUCTIONS>\n"
            "<skills_instructions>Safety first.</skills_instructions>",
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    assert rendered[0].content == ""
    assert len(rendered[0].content_blocks) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls is not None
    assert [call.arguments for call in block.tool_calls] == [
        {"tag": "instructions"},
        {"tag": "skills_instructions"},
    ]
    assert [call.result.content for call in block.tool_calls if call.result is not None] == [
        "# Repository Guidelines",
        "Safety first.",
    ]


def test_render_incremental_returns_completed_turns():
    state = RenderState()

    # Turn 1: User
    completed, state = render_incremental([make_msg(0, "user", "hi")], state)
    assert len(completed) == 0  # User turn not finished until next turn starts or transcript ends

    # Turn 2: Assistant starts
    completed, state = render_incremental([make_msg(1, "assistant", "Hello")], state)
    assert len(completed) == 1
    assert completed[0].role == "user"

    # Turn 2: Assistant continues
    completed, state = render_incremental([make_msg(2, "assistant", "!")], state)
    assert len(completed) == 0

    # Turn 3: User starts
    completed, state = render_incremental([make_msg(3, "user", "how are you")], state)
    assert len(completed) == 1
    assert completed[0].role == "assistant"
    assert completed[0].content == "Hello!"


def test_render_transcript_unknown_block_type():
    msg = make_msg(0, "assistant", "special", content_type="random_type")
    msg.raw_json = {"some": "raw"}

    rendered = render_transcript([msg])
    assert len(rendered) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "unknown"
    assert block.block_type == "random_type"
    assert block.raw == {"some": "raw"}


def test_render_transcript_suppresses_hook_prompt_blocks():
    msgs = [
        make_msg(0, "assistant", "internal", content_type="hook_prompt"),
        make_msg(1, "assistant", "Visible output"),
    ]

    rendered = render_transcript(msgs)

    assert len(rendered) == 1
    assert rendered[0].content == "Visible output"
    assert [block.type for block in rendered[0].content_blocks] == ["text"]


def test_render_transcript_tool_reference():
    msgs = [
        make_msg(0, "assistant", "mcp__server__tool", content_type="tool_reference"),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_reference"
    assert block.tool_name == "mcp__server__tool"
    assert block.server_name == "server"
    assert block.content is None


def test_render_transcript_image():
    image_source = {"type": "base64", "media_type": "image/png", "data": "abc"}
    msg = make_msg(0, "assistant", image_source, content_type="image")
    rendered = render_transcript([msg])
    assert len(rendered) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "image"
    assert block.source == image_source
    assert block.content is None


def test_classify_tool():
    assert classify_tool("Bash") == ("bash", None)
    assert classify_tool("exec_command") == ("bash", None)
    assert classify_tool("Read") == ("read", None)
    assert classify_tool("Edit") == ("edit", None)
    assert classify_tool("MultiEdit") == ("edit", None)
    assert classify_tool("mcp__server__tool") == ("mcp", "server")
    assert classify_tool("mcp__other") == ("mcp", "unknown")
    assert classify_tool("Unknown") == ("unknown", None)
    assert classify_tool(None) == ("unknown", None)


def test_extract_result_metadata():
    # Bash
    bash_res = {"exit_code": 0, "stdout": "line1\nline2", "stderr": "error1"}
    meta = extract_result_metadata("bash", bash_res)
    assert meta["exit_code"] == 0
    assert meta["stdout_lines"] == 2
    assert meta["stderr_lines"] == 1

    # Read
    read_res = "line1\nline2\nline3"
    meta = extract_result_metadata("read", read_res, {"file_path": "test.py"})
    assert meta["line_count"] == 3
    assert meta["file_path"] == "test.py"

    # Edit
    meta = extract_result_metadata("edit", "ok", {"path": "test.py"})
    assert meta["file_path"] == "test.py"

    # Grep
    grep_res = {"files_matched": 5, "total_matches": 10}
    meta = extract_result_metadata("grep", grep_res)
    assert meta["files_matched"] == 5
    assert meta["total_matches"] == 10

    # Glob
    glob_res = ["file1.py", "file2.py"]
    meta = extract_result_metadata("glob", glob_res)
    assert meta["files_found"] == 2

    # Default
    assert extract_result_metadata("unknown", "something") == {}


def test_render_transcript_metadata_integration():
    msgs = [
        make_msg(
            0,
            "assistant",
            "",
            content_type="tool_use",
            tool_use_id="call-1",
            tool_name="Bash",
            tool_input={"command": "ls"},
        ),
        make_msg(
            1,
            "user",
            "",
            content_type="tool_result",
            tool_use_id="call-1",
            tool_result={"exit_code": 0, "stdout": "a\nb", "stderr": ""},
        ),
    ]
    rendered = render_transcript(msgs)
    assert len(rendered) == 1
    block = rendered[0].content_blocks[0]
    tool_call = block.tool_calls[0]
    assert tool_call.tool_type == "bash"
    assert tool_call.result.metadata["exit_code"] == 0
    assert tool_call.result.metadata["stdout_lines"] == 2
