import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import Mock

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


def test_concurrent_sequence_allocation_is_unique(temp_db: HubDatabase) -> None:
    worker_count = 8
    start = threading.Barrier(worker_count)

    def save(index: int) -> str:
        start.wait()
        return chat_messages.save_message(
            temp_db,
            conversation_id="concurrent-conversation",
            role="user",
            content=f"message-{index}",
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        message_ids = list(executor.map(save, range(worker_count)))

    messages = chat_messages.get_messages(temp_db, "concurrent-conversation")

    assert len(set(message_ids)) == worker_count
    assert [message["seq"] for message in messages] == list(range(1, worker_count + 1))


def test_get_messages_orders_by_sequence_then_id() -> None:
    db = Mock(spec=HubDatabase)
    db.fetchall.return_value = []

    chat_messages.get_messages(cast(HubDatabase, db), "conversation")

    sql = " ".join(db.fetchall.call_args.args[0].split())
    assert "ORDER BY seq ASC, id ASC" in sql
