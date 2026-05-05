from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks


def test_assistant_content_blocks_interleave_text_thinking_and_tools() -> None:
    blocks = AssistantContentBlocks()

    blocks.append_text("First")
    blocks.append_thinking("Think")
    blocks.append_tool_call(
        tool_call_id="tool-1",
        tool_name="Bash",
        server_name="builtin",
        arguments={"command": "pwd"},
    )
    blocks.complete_tool_call(
        tool_call_id="tool-1",
        success=True,
        result={"content": "/tmp/project"},
    )
    blocks.append_text("Done")

    assert blocks.visible_text == "FirstDone"
    assert [block["type"] for block in blocks.blocks] == [
        "text",
        "thinking",
        "tool_chain",
        "text",
    ]
    tool_call = blocks.blocks[2]["tool_calls"][0]
    assert tool_call["status"] == "completed"
    assert tool_call["tool_type"] == "bash"
