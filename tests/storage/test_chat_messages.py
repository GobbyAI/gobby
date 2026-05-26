import json

from gobby.storage import chat_messages
from gobby.storage.hub.protocol import HubDatabase


def test_chat_messages_round_trip_content_blocks(temp_db: HubDatabase) -> None:
    db = temp_db
    blocks = [
        {"type": "text", "content": "Looking"},
        {"type": "thinking", "content": "Check state"},
        {
            "type": "tool_chain",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "tool_name": "Bash",
                    "server_name": "builtin",
                    "tool_type": "bash",
                    "status": "completed",
                    "arguments": {"command": "pwd"},
                    "result": {"content": "/tmp/project"},
                }
            ],
        },
        {"type": "text", "content": "Done"},
    ]

    chat_messages.save_message(
        db,
        conversation_id="conv-1",
        role="assistant",
        content="LookingDone",
        content_blocks_json=json.dumps(blocks),
    )

    messages = chat_messages.get_messages(db, "conv-1")

    assert messages[0]["content_blocks"] == blocks
