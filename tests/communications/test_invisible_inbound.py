"""Trusted DMs stay intact; forwarded and group text is cleaned."""

import pytest

from gobby.communications.adapters.telegram_inbound import parse_telegram_update
from gobby.communications.telegram_callbacks import TelegramCallbackRegistry

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "chat_type,forwarded,expected",
    [("private", False, "hello\u202e"), ("private", True, "hello"), ("group", False, "hello")],
)
@pytest.mark.parametrize("field", ["text", "caption"])
def test_inbound_trust_boundary(chat_type: str, forwarded: bool, expected: str, field: str) -> None:
    message = {
        "chat": {"id": 1, "type": chat_type},
        "from": {"id": 2},
        "message_id": 3,
        field: "hello\u202e",
    }
    if field == "caption":
        message["document"] = {"file_id": "file", "file_name": "sample.txt", "file_size": 2}
    if forwarded:
        message["forward_origin"] = {"type": "user"}
    messages = parse_telegram_update(
        {"message": message},
        bot_username=None,
        bot_user_id=None,
        callback_registry=TelegramCallbackRegistry(),
    )
    assert len(messages) == 1
    assert messages[0].content == expected


def test_invisible_only_untrusted_message_is_discarded() -> None:
    messages = parse_telegram_update(
        {
            "message": {
                "chat": {"id": 1, "type": "group"},
                "from": {"id": 2},
                "message_id": 3,
                "text": "\u202e\U000e0061\x00",
            }
        },
        bot_username=None,
        bot_user_id=None,
        callback_registry=TelegramCallbackRegistry(),
    )
    assert messages == []
