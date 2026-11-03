"""send_attachment and get_attachment: delivery, read access, and the size cap."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_attachments import MAX_ATTACHMENT_BYTES
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.integration


class FakeWakeDispatcher:
    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        return {"session_id": session_id, "delivered": True, "method": "fake"}


@dataclass
class _Env:
    registry: InternalToolRegistry
    ids: dict[str, str]
    db: HubDatabase


@pytest.fixture
def env(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Env:
    sessions = SessionManager(temp_db)
    ids: dict[str, str] = {}
    for name in ("root", "peer", "other"):
        ids[name] = sessions.register(
            external_id=name,
            machine_id=require_machine_id(),
            source="claude",
            project_id=sample_project["id"],
        ).id
    # Outside the sender's project: a project fanout never reaches this session.
    ids["outsider"] = sessions.register(
        external_id="outsider",
        machine_id=require_machine_id(),
        source="claude",
        project_id=None,
    ).id

    registry = InternalToolRegistry(name="gobby-agents", description="Agent messaging v2")
    add_messaging_tools(
        registry=registry,
        message_manager=InterSessionMessageManager(temp_db),
        session_manager=sessions,
        db=temp_db,
        wake_dispatcher=FakeWakeDispatcher(),
    )
    return _Env(registry=registry, ids=ids, db=temp_db)


async def _call(env: _Env, caller: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    with session_context_for_test(env.ids[caller]):
        result: dict[str, Any] = await env.registry.call(tool, arguments)
    return result


async def _send_document(env: _Env, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "target": "session",
        "target_id": env.ids["peer"],
        "content": "review attached",
        "filename": "review.md",
        "document": "# Review\n\nLGTM for the candidate.",
        "media_type": "text/markdown",
        "wake": False,
        **overrides,
    }
    return await _call(env, "root", "send_attachment", arguments)


def _stored_message_count(env: _Env, sender: str) -> int:
    row = env.db.fetchone(
        "SELECT count(*) AS n FROM inter_session_messages WHERE from_session = %s",
        (env.ids[sender],),
    )
    assert row is not None
    return int(row["n"])


@pytest.mark.asyncio
class TestSendAttachment:
    async def test_recipient_reads_the_exact_document_back(self, env: _Env) -> None:
        sent = await _send_document(env)

        assert sent["success"] is True
        assert len(sent["message_ids"]) == 1
        assert sent["attachment"]["filename"] == "review.md"

        read = await _call(env, "peer", "get_attachment", {"message_id": sent["message_ids"][0]})
        assert read["success"] is True
        assert read["attachment"]["content"] == "# Review\n\nLGTM for the candidate."
        assert read["attachment"]["sha256"] == sent["attachment"]["sha256"]
        assert read["attachment"]["size_bytes"] == sent["attachment"]["size_bytes"]

    async def test_sender_can_read_back_its_own_attachment(self, env: _Env) -> None:
        sent = await _send_document(env)

        read = await _call(env, "root", "get_attachment", {"message_id": sent["message_ids"][0]})

        assert read["success"] is True
        assert read["attachment"]["filename"] == "review.md"

    async def test_non_recipient_is_refused_exactly_like_a_missing_message(self, env: _Env) -> None:
        sent = await _send_document(env)

        refused = await _call(
            env, "outsider", "get_attachment", {"message_id": sent["message_ids"][0]}
        )
        missing = await _call(env, "outsider", "get_attachment", {"message_id": str(uuid.uuid4())})

        assert refused["success"] is False
        assert refused["error_code"] == "attachment_not_found"
        assert refused == missing
        assert sent["message_ids"][0] not in str(refused)

    async def test_project_fanout_gives_each_recipient_its_own_copy(self, env: _Env) -> None:
        sent = await _send_document(env, target="project", target_id=None)

        assert sent["success"] is True
        assert len(sent["message_ids"]) == 2
        for recipient in ("peer", "other"):
            readable = []
            for message_id in sent["message_ids"]:
                read = await _call(env, recipient, "get_attachment", {"message_id": message_id})
                if read["success"]:
                    readable.append(read)
            assert len(readable) == 1
            assert readable[0]["attachment"]["sha256"] == sent["attachment"]["sha256"]

        outsider_read = await _call(
            env, "outsider", "get_attachment", {"message_id": sent["message_ids"][0]}
        )
        assert outsider_read["error_code"] == "attachment_not_found"

    async def test_global_fanout_gives_each_recipient_its_own_copy(self, env: _Env) -> None:
        sent = await _send_document(env, target="global", target_id=None)

        assert sent["success"] is True
        assert len(sent["message_ids"]) == 3
        for recipient in ("peer", "other", "outsider"):
            readable = []
            for message_id in sent["message_ids"]:
                read = await _call(env, recipient, "get_attachment", {"message_id": message_id})
                if read["success"]:
                    readable.append(read)
            assert len(readable) == 1
            assert readable[0]["attachment"]["sha256"] == sent["attachment"]["sha256"]

    async def test_oversize_document_is_refused_and_nothing_is_stored(self, env: _Env) -> None:
        before = _stored_message_count(env, "root")

        refused = await _send_document(env, document="a" * (MAX_ATTACHMENT_BYTES + 1))

        assert refused["success"] is False
        assert refused["error_code"] == "attachment_too_large"
        assert _stored_message_count(env, "root") == before

    async def test_unsafe_filename_is_refused_and_nothing_is_stored(self, env: _Env) -> None:
        before = _stored_message_count(env, "root")

        refused = await _send_document(env, filename="../escape.md")

        assert refused["success"] is False
        assert refused["error_code"] == "attachment_invalid_filename"
        assert _stored_message_count(env, "root") == before

    async def test_content_is_still_required_for_the_message(self, env: _Env) -> None:
        refused = await _send_document(env, content="")

        assert refused["success"] is False
        assert "content is required" in refused["error"]

    async def test_recipient_message_retrieval_names_the_attachment_without_content(
        self, env: _Env
    ) -> None:
        sent = await _send_document(env)
        message_id = sent["message_ids"][0]

        retrieved = await _call(
            env, "peer", "get_inter_session_message", {"message_id": message_id}
        )

        assert retrieved["success"] is True
        assert retrieved["attachment"]["filename"] == "review.md"
        assert retrieved["attachment"]["sha256"] == sent["attachment"]["sha256"]
        assert "content" not in retrieved["attachment"]

    async def test_outsider_message_retrieval_stays_refused_and_unannotated(
        self, env: _Env
    ) -> None:
        sent = await _send_document(env)

        retrieved = await _call(
            env, "outsider", "get_inter_session_message", {"message_id": sent["message_ids"][0]}
        )

        assert retrieved["success"] is False
        assert "attachment" not in retrieved
