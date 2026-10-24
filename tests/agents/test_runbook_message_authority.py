"""Plan 3.4: who may address the Orchestrator, and Triage digest delivery (mailbox level)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.sessions.mailbox import MailboxSendResult, MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.conftest import AGENT_TEST_MACHINE_ID

pytestmark = pytest.mark.unit

REFUSED = "message_recipient_not_allowed"


class _Wakes:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


def _seat(
    sessions: SessionManager,
    project_id: str,
    name: str,
    role: str,
    *,
    parent: str | None = None,
) -> str:
    session = sessions.register(
        external_id=name,
        machine_id=AGENT_TEST_MACHINE_ID,
        source="codex",
        project_id=project_id,
        title=name,
        agent_depth=1 if parent else 0,
        parent_session_id=parent,
    )
    SessionVariableManager(sessions.db).set_variable(session.id, "_agent_type", role)
    return session.id


def _mailbox(db: HubDatabase, sessions: SessionManager, wakes: _Wakes) -> MailboxService:
    return MailboxService(
        db=db,
        message_manager=InterSessionMessageManager(db),
        session_manager=sessions,
        wake_dispatcher=wakes,
    )


def _metadata(db: HubDatabase, result: MailboxSendResult) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT metadata_json FROM inter_session_messages WHERE id = %s", (result.message_ids[0],)
    )
    assert row is not None
    return dict(json.loads(row["metadata_json"]))


@pytest.mark.asyncio
async def test_resolved_role_routing(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    orchestrator = _seat(session_manager, project, "orchestrator", "orchestrator")
    assistant = _seat(session_manager, project, "assistant", "assistant")
    operator = _seat(session_manager, project, "operator", "default")
    triage = _seat(session_manager, project, "triage", "triage", parent=orchestrator)
    manager = _seat(session_manager, project, "dev-manager", "dev-manager", parent=orchestrator)
    developer = _seat(session_manager, project, "developer", "developer", parent=orchestrator)
    worker = _seat(session_manager, project, "worker", "default", parent=orchestrator)
    pending = LocalAgentRunManager(temp_db).create(
        parent_session_id=orchestrator, provider="codex", prompt="pending seat"
    )
    mailbox = _mailbox(temp_db, session_manager, _Wakes())

    async def send(sender: str, target: str, target_id: str | None = None) -> MailboxSendResult:
        return await mailbox.send(
            from_session_id=sender, target=target, target_id=target_id, content="report"
        )

    # Team seats, spawned default workers and alternate direct selectors cannot reach it.
    for sender, target, target_id in (
        (developer, "session", orchestrator),
        (manager, "session", orchestrator),
        (worker, "session", orchestrator),
        (developer, "parent", None),
        (developer, "agent", pending.id),
    ):
        refused = await send(sender, target, target_id)
        assert (refused.error_code, refused.recipient_session_ids) == (REFUSED, [])
        assert refused.success is False
    assert temp_db.fetchone(
        "SELECT count(*) AS n FROM inter_session_messages WHERE to_session = %s",
        (orchestrator,),
    ) == {"n": 0}

    # Assistant, Triage, the operator's bare terminal and the daemon reach it.
    for sender in (assistant, triage, operator, orchestrator):
        delivered = await send(sender, "session", orchestrator)
        assert delivered.recipient_session_ids == [orchestrator]
    notice = await mailbox.send(
        from_session_id=system_session_id(),
        target="session",
        target_id=orchestrator,
        project_id=project,
        content="daemon notice",
    )
    assert notice.recipient_session_ids == [orchestrator]

    # Every seat reports to Triage and its Dev Manager; peer coordination stays open.
    for sender, recipient in ((developer, triage), (developer, manager), (manager, developer)):
        assert (await send(sender, "session", recipient)).recipient_session_ids == [recipient]

    # Fanout withholds only the Orchestrator, and only from senders that may not address it.
    for target in ("project", "global"):
        fanout = await send(developer, target)
        assert fanout.success is True
        assert orchestrator not in fanout.recipient_session_ids
        assert {triage, manager, assistant}.issubset(fanout.recipient_session_ids)
        assert orchestrator in (await send(triage, target)).recipient_session_ids


@pytest.mark.asyncio
async def test_triage_digest_is_queued_and_relaunch_safe(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    stale_orchestrator = _seat(session_manager, project, "orchestrator-old", "orchestrator")
    session_manager.update_status(stale_orchestrator, "expired")
    orchestrator = _seat(session_manager, project, "orchestrator-new", "orchestrator")
    stale_triage = _seat(session_manager, project, "triage-old", "triage", parent=orchestrator)
    session_manager.update_status(stale_triage, "expired")
    triage = _seat(session_manager, project, "triage-new", "triage", parent=orchestrator)
    stale_developer = _seat(
        session_manager, project, "developer-old", "developer", parent=orchestrator
    )
    session_manager.update_status(stale_developer, "expired")
    developer = _seat(
        session_manager, project, "developer-other-team", "developer", parent=orchestrator
    )
    wakes = _Wakes()
    mailbox = _mailbox(temp_db, session_manager, wakes)

    # wake=None is a sender that omitted wake. The digest to a stale Orchestrator ref
    # reaches the relaunched Orchestrator and stays queued.
    digest = await mailbox.send(
        from_session_id=triage,
        target="session",
        target_id=stale_orchestrator,
        content="digest",
        wake=None,
    )
    assert digest.recipient_session_ids == [orchestrator]
    assert digest.wake_results == []
    assert wakes.calls == []
    metadata = _metadata(temp_db, digest)
    assert (metadata["redirected_from"], metadata["wake_requested"]) == (
        stale_orchestrator,
        False,
    )

    # An operator-requested immediate wake is honoured.
    urgent = await mailbox.send(
        from_session_id=triage,
        target="session",
        target_id=orchestrator,
        content="operator asked for an immediate wake",
        wake=True,
    )
    assert urgent.recipient_session_ids == [orchestrator]
    assert wakes.calls == [orchestrator]

    # A seat's report to a stale Triage ref follows the relaunch and keeps the direct wake.
    report = await mailbox.send(
        from_session_id=developer,
        target="session",
        target_id=stale_triage,
        content="blocked",
        wake=None,
    )
    assert report.recipient_session_ids == [triage]
    assert wakes.calls == [orchestrator, triage]

    # Team roles have no project-wide binding: a stale developer ref never lands on
    # another team's developer.
    other_team = await mailbox.send(
        from_session_id=triage,
        target="session",
        target_id=stale_developer,
        content="stale seat",
        wake=False,
    )
    assert other_team.recipient_session_ids == [stale_developer]

    # Two live Orchestrators make the binding ambiguous, so the stale ref is kept.
    _seat(session_manager, project, "orchestrator-second", "orchestrator")
    ambiguous = await mailbox.send(
        from_session_id=triage,
        target="session",
        target_id=stale_orchestrator,
        content="digest",
        wake=None,
    )
    assert ambiguous.recipient_session_ids == [stale_orchestrator]
