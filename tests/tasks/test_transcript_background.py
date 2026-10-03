"""Claude background-receipt notifications parse only plain, declaration-free XML."""

from datetime import UTC, datetime

import pytest

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.tasks.transcript_background import _notification

pytestmark = pytest.mark.unit

_FIELDS = (
    "<task-id>job-1</task-id><tool-use-id>toolu-1</tool-use-id>"
    "<output-file>/tmp/job-1.out</output-file><status>failed</status>"
    '<summary>Background command "pytest" failed with exit code 1</summary>'
)


def _message(content: str) -> ParsedMessage:
    return ParsedMessage(
        index=0,
        role="user",
        content=content,
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        raw_json={},
    )


def test_plain_notification_yields_its_fields() -> None:
    notice = _notification(_message(f"<task-notification>{_FIELDS}</task-notification>"))
    assert notice == {
        "task-id": "job-1",
        "tool-use-id": "toolu-1",
        "output-file": "/tmp/job-1.out",
        "status": "failed",
        "summary": 'Background command "pytest" failed with exit code 1',
    }


@pytest.mark.parametrize(
    "content",
    [
        # A DTD after the root tag opens, carrying an internal entity expansion.
        f'<task-notification><!DOCTYPE x [<!ENTITY e "job-1">]>{_FIELDS}</task-notification>',
        # An external entity reference smuggled into a field.
        '<task-notification><!ENTITY xxe SYSTEM "file:///etc/passwd">'
        f"{_FIELDS}<summary>&xxe;</summary></task-notification>",
    ],
    ids=["internal-entity", "external-entity"],
)
def test_notification_with_a_declaration_is_rejected(content: str) -> None:
    assert _notification(_message(content)) is None
