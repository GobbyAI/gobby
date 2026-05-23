import json

from gobby.storage import chat_messages
from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations


def test_chat_messages_round_trip_content_blocks(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "chat_messages.db")
    run_migrations(db)

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
