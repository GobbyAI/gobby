"""Attachments stored with inter-session messages: validation, storage, read access."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentDraft,
    AttachmentError,
    InterSessionAttachmentManager,
)
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000005"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.mark.unit
class TestAttachmentDraft:
    def test_build_records_size_and_sha256_of_the_utf8_bytes(self) -> None:
        content = "# Review\n\nLooks good. Café ☕\n"
        draft = AttachmentDraft.build(
            filename="review.md", media_type="text/markdown", content=content
        )

        encoded = content.encode("utf-8")
        assert draft.size_bytes == len(encoded)
        assert draft.sha256 == hashlib.sha256(encoded).hexdigest()
        assert draft.content == content
        assert draft.filename == "review.md"
        assert draft.media_type == "text/markdown"

    def test_build_accepts_a_document_exactly_at_the_cap(self) -> None:
        draft = AttachmentDraft.build(
            filename="big.txt",
            media_type="text/plain",
            content="a" * MAX_ATTACHMENT_BYTES,
        )
        assert draft.size_bytes == MAX_ATTACHMENT_BYTES

    def test_build_refuses_one_byte_over_the_cap_with_a_typed_error(self) -> None:
        with pytest.raises(AttachmentError) as refused:
            AttachmentDraft.build(
                filename="big.txt",
                media_type="text/plain",
                content="a" * (MAX_ATTACHMENT_BYTES + 1),
            )
        assert refused.value.code == "attachment_too_large"

    def test_cap_counts_utf8_bytes_not_characters(self) -> None:
        # Half the cap in characters is more than the cap in bytes.
        content = "é" * (MAX_ATTACHMENT_BYTES // 2 + 1)
        with pytest.raises(AttachmentError) as refused:
            AttachmentDraft.build(filename="wide.md", media_type="text/markdown", content=content)
        assert refused.value.code == "attachment_too_large"

    def test_build_refuses_empty_content(self) -> None:
        with pytest.raises(AttachmentError) as refused:
            AttachmentDraft.build(filename="empty.md", media_type="text/markdown", content="")
        assert refused.value.code == "attachment_empty"

    @pytest.mark.parametrize(
        "filename",
        [
            "",
            "   ",
            ".",
            "..",
            "../secrets.md",
            "reports/review.md",
            "reports\\review.md",
            "bad\nname.md",
            "tab\tname.md",
            "x" * 256,
        ],
    )
    def test_build_refuses_unsafe_filenames(self, filename: str) -> None:
        with pytest.raises(AttachmentError) as refused:
            AttachmentDraft.build(filename=filename, media_type="text/markdown", content="body")
        assert refused.value.code == "attachment_invalid_filename"

    @pytest.mark.parametrize("media_type", ["application/pdf", "text/html", "", "TEXT/PLAIN"])
    def test_build_refuses_media_types_outside_text_and_markdown(self, media_type: str) -> None:
        with pytest.raises(AttachmentError) as refused:
            AttachmentDraft.build(filename="doc", media_type=media_type, content="body")
        assert refused.value.code == "attachment_invalid_media_type"

    def test_summary_names_the_document_without_its_content(self) -> None:
        draft = AttachmentDraft.build(
            filename="plan.md", media_type="text/markdown", content="# Plan"
        )
        assert draft.summary() == {
            "filename": "plan.md",
            "media_type": "text/markdown",
            "size_bytes": draft.size_bytes,
            "sha256": draft.sha256,
        }


@pytest.fixture
def exchange(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]]:
    project = isolated_checkout_factory(temp_db, "attachment-storage").project
    sessions = SessionManager(temp_db)
    ids = {
        name: sessions.register(
            external_id=f"attach-{name}",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project.id,
        ).id
        for name in ("sender", "recipient", "stranger")
    }
    return (
        InterSessionMessageManager(temp_db),
        InterSessionAttachmentManager(temp_db),
        ids,
    )


def _draft(content: str = "# Review\n\nLGTM") -> AttachmentDraft:
    return AttachmentDraft.build(filename="review.md", media_type="text/markdown", content=content)


@pytest.mark.integration
class TestAttachmentStorage:
    def test_sender_and_recipient_read_back_the_stored_document(
        self,
        exchange: tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]],
    ) -> None:
        messages, attachments, ids = exchange
        message = messages.create_message(
            from_session=ids["sender"], to_session=ids["recipient"], content="see attached"
        )
        draft = _draft()
        attachments.create(message.id, draft)

        for reader in (ids["sender"], ids["recipient"]):
            stored = attachments.get_for_caller(message.id, reader)
            assert stored is not None
            assert stored.content == draft.content
            assert stored.filename == "review.md"
            assert stored.media_type == "text/markdown"
            assert stored.size_bytes == draft.size_bytes
            assert stored.sha256 == draft.sha256

    def test_a_session_outside_the_message_gets_nothing(
        self,
        exchange: tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]],
    ) -> None:
        messages, attachments, ids = exchange
        message = messages.create_message(
            from_session=ids["sender"], to_session=ids["recipient"], content="private"
        )
        attachments.create(message.id, _draft())

        assert attachments.get_for_caller(message.id, ids["stranger"]) is None

    def test_missing_and_malformed_ids_read_as_absent(
        self,
        exchange: tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]],
    ) -> None:
        _, attachments, ids = exchange

        assert attachments.get_for_caller(str(uuid.uuid4()), ids["sender"]) is None
        assert attachments.get_for_caller("not-a-uuid", ids["sender"]) is None

    def test_summary_carries_the_digest_but_not_the_content(
        self,
        exchange: tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]],
    ) -> None:
        messages, attachments, ids = exchange
        message = messages.create_message(
            from_session=ids["sender"], to_session=ids["recipient"], content="see attached"
        )
        draft = _draft()
        attachments.create(message.id, draft)

        summary = attachments.get_summary(message.id)
        assert summary == draft.summary()

    def test_deleting_the_message_deletes_its_attachment(
        self,
        exchange: tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]],
        temp_db: HubDatabase,
    ) -> None:
        messages, attachments, ids = exchange
        message = messages.create_message(
            from_session=ids["sender"], to_session=ids["recipient"], content="see attached"
        )
        attachments.create(message.id, _draft())

        temp_db.execute("DELETE FROM inter_session_messages WHERE id = %s", (message.id,))

        assert attachments.get_summary(message.id) is None

    def test_retention_of_a_delivered_message_removes_its_attachment(
        self,
        exchange: tuple[InterSessionMessageManager, InterSessionAttachmentManager, dict[str, str]],
    ) -> None:
        messages, attachments, ids = exchange
        message = messages.create_message(
            from_session=ids["sender"], to_session=ids["recipient"], content="see attached"
        )
        attachments.create(message.id, _draft())
        messages.mark_delivered(message.id, ids["recipient"])

        deleted = messages.delete_delivered_before(datetime.now(UTC) + timedelta(minutes=1))

        assert deleted >= 1
        assert attachments.get_summary(message.id) is None
