"""Definition-controlled recipient admission, direct wakes, and relaunch routing."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Literal

import pytest

from gobby.sessions.mailbox import MailboxSendResult, MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager, AgentDefinitionRow
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
    capabilities: tuple[str, ...] = (),
    required_senders: tuple[str, ...] = (),
) -> str:
    definitions = AgentDefinitionManager(sessions.db)
    if definitions.get_by_name(role, project_id) is None:
        definitions.create(
            role,
            {
                "name": role,
                "prompts": {"agent": "Coordinate the assigned work."},
                "workflows": {"rule_selectors": {"include": []}},
                "capabilities": list(capabilities),
                "message_sender_capabilities": list(required_senders),
            },
            project_id=project_id,
        )
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
    orchestrator = _seat(
        session_manager,
        project,
        "orchestrator",
        "orchestrator",
        capabilities=("send_reports",),
        required_senders=("send_reports",),
    )
    assistant = _seat(
        session_manager, project, "assistant", "assistant", capabilities=("send_reports",)
    )
    operator = _seat(session_manager, project, "operator", "default")
    triage = _seat(
        session_manager,
        project,
        "triage",
        "triage",
        parent=orchestrator,
        capabilities=("send_reports",),
    )
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
async def test_declared_standing_seats_relaunch_and_direct_messages_wake(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    stale_orchestrator = _seat(
        session_manager,
        project,
        "orchestrator-old",
        "orchestrator",
        capabilities=("follow_relaunch",),
        required_senders=("send_reports",),
    )
    session_manager.update_status(stale_orchestrator, "expired")
    orchestrator = _seat(session_manager, project, "orchestrator-new", "orchestrator")
    stale_triage = _seat(
        session_manager,
        project,
        "triage-old",
        "triage",
        parent=orchestrator,
        capabilities=("follow_relaunch", "send_reports"),
    )
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
    # reaches the relaunched Orchestrator and wakes it.
    digest = await mailbox.send(
        from_session_id=triage,
        target="session",
        target_id=stale_orchestrator,
        content="digest",
        wake=None,
    )
    assert digest.recipient_session_ids == [orchestrator]
    assert digest.wake_results[0]["delivered"] is True
    assert wakes.calls == [orchestrator]
    metadata = _metadata(temp_db, digest)
    assert (metadata["redirected_from"], metadata["wake_requested"]) == (
        stale_orchestrator,
        True,
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
    assert wakes.calls == [orchestrator, orchestrator]

    # A seat's report to a stale Triage ref follows the relaunch and keeps the direct wake.
    report = await mailbox.send(
        from_session_id=developer,
        target="session",
        target_id=stale_triage,
        content="blocked",
        wake=None,
    )
    assert report.recipient_session_ids == [triage]
    assert wakes.calls == [orchestrator, orchestrator, triage]

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


@pytest.mark.asyncio
async def test_recipient_permissions_follow_definitions_not_names(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    recipient = _seat(
        session_manager, project, "gate", "custom-inbox", required_senders=("send_reports",)
    )
    admitted = _seat(
        session_manager, project, "reporter", "custom-reporter", capabilities=("send_reports",)
    )
    refused = _seat(session_manager, project, "ordinary", "triage", parent=recipient)
    wakes = _Wakes()
    mailbox = _mailbox(temp_db, session_manager, wakes)

    denied = await mailbox.send(
        from_session_id=refused,
        target="session",
        target_id=recipient,
        content="not authorized by the definition",
        wake=None,
    )
    assert denied.error_code == REFUSED
    assert denied.messages == []
    delivered = await mailbox.send(
        from_session_id=admitted,
        target="session",
        target_id=recipient,
        content="report",
        wake=None,
    )
    assert delivered.recipient_session_ids == [recipient]
    assert wakes.calls == [recipient]
    fanout = await mailbox.send(from_session_id=refused, target="project", content="broadcast")
    assert recipient not in fanout.recipient_session_ids
    assert admitted in fanout.recipient_session_ids

    definitions = AgentDefinitionManager(temp_db)
    definition = definitions.get_by_name("custom-reporter", project)
    assert definition is not None
    definitions.toggle_enabled(definition.id)
    disabled = await mailbox.send(
        from_session_id=admitted, target="session", target_id=recipient, content="disabled grant"
    )
    assert disabled.error_code == REFUSED
    assert disabled.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "disabled", "malformed"])
async def test_unavailable_recipient_definition_refuses_delivery(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    state: Literal["missing", "disabled", "malformed"],
) -> None:
    project = sample_project["id"]
    recipient = _seat(
        session_manager,
        project,
        "gated-recipient",
        "gated-inbox",
        required_senders=("send_reports",),
    )
    sender = _seat(session_manager, project, "ordinary-sender", "ordinary-sender")
    definitions = AgentDefinitionManager(temp_db)
    definition = definitions.get_by_name("gated-inbox", project)
    assert definition is not None
    if state == "missing":
        definitions.delete(definition.id)
    elif state == "disabled":
        definitions.toggle_enabled(definition.id)
    else:
        definitions.update(
            definition.id,
            definition_json={**definition.definition_json, "message_sender_capabilities": [1]},
        )
    wakes = _Wakes()
    mailbox = _mailbox(temp_db, session_manager, wakes)

    result = await mailbox.send(
        from_session_id=sender, target="session", target_id=recipient, content="report"
    )

    assert result.error_code == REFUSED
    assert result.messages == []
    assert wakes.calls == []
    assert temp_db.fetchone(
        "SELECT count(*) AS n FROM inter_session_messages WHERE to_session = %s", (recipient,)
    ) == {"n": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_role", ["reporter", "inbox"])
async def test_retired_definition_fields_refuse_delivery_without_aborting_fanout(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    broken_role: Literal["reporter", "inbox"],
) -> None:
    project = sample_project["id"]
    sender = _seat(session_manager, project, "reporter", "reporter", capabilities=("send_reports",))
    recipient = _seat(
        session_manager, project, "inbox", "inbox", required_senders=("send_reports",)
    )
    peer = _seat(session_manager, project, "peer", "open-inbox")
    definitions = AgentDefinitionManager(temp_db)
    row = definitions.get_by_name(broken_role, project)
    assert row is not None
    stored = replace(row, definition_json={**row.definition_json, "terminal_backend": "tmux"})
    original_lookup = AgentDefinitionManager.get_by_name

    def lookup(
        manager: AgentDefinitionManager,
        name: str,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> AgentDefinitionRow | None:
        if name == broken_role and project_id == project:
            return stored
        return original_lookup(manager, name, project_id, include_deleted)

    monkeypatch.setattr(AgentDefinitionManager, "get_by_name", lookup)
    wakes = _Wakes()
    mailbox = _mailbox(temp_db, session_manager, wakes)

    direct = await mailbox.send(
        from_session_id=sender, target="session", target_id=recipient, content="report"
    )
    assert direct.error_code == REFUSED
    assert direct.messages == []
    assert wakes.calls == []

    fanout = await mailbox.send(from_session_id=sender, target="project", content="broadcast")
    assert fanout.success is True
    assert recipient not in fanout.recipient_session_ids
    assert peer in fanout.recipient_session_ids


@pytest.mark.asyncio
async def test_unseated_child_has_no_operator_message_exemption(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    parent = _seat(session_manager, project, "parent", "reporter")
    child = session_manager.register(
        external_id="unseated-child",
        source="codex",
        project_id=project,
        machine_id=AGENT_TEST_MACHINE_ID,
        parent_session_id=parent,
    )
    recipient = _seat(session_manager, project, "recipient", "inbox", required_senders=("report",))
    wakes = _Wakes()

    result = await _mailbox(temp_db, session_manager, wakes).send(
        from_session_id=child.id, target="session", target_id=recipient, content="report"
    )

    assert result.error_code == REFUSED
    assert result.messages == []
    assert wakes.calls == []


@pytest.mark.asyncio
async def test_operator_recipient_needs_no_agent_definition(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    sender = _seat(session_manager, project, "sender-to-operator", "ordinary-sender")
    operator = session_manager.register(
        external_id="bare-operator",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="codex",
        project_id=project,
    )
    mailbox = _mailbox(temp_db, session_manager, _Wakes())

    result = await mailbox.send(
        from_session_id=sender, target="session", target_id=operator.id, content="report"
    )

    assert result.recipient_session_ids == [operator.id]


@pytest.mark.asyncio
async def test_definition_can_follow_a_relaunched_custom_seat(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    previous = _seat(
        session_manager, project, "previous", "custom-inbox", capabilities=("follow_relaunch",)
    )
    session_manager.update_status(previous, "expired")
    current = _seat(session_manager, project, "current", "custom-inbox")
    sender = _seat(session_manager, project, "sender", "custom-sender")
    mailbox = _mailbox(temp_db, session_manager, _Wakes())
    result = await mailbox.send(
        from_session_id=sender, target="session", target_id=previous, content="report", wake=None
    )
    assert result.recipient_session_ids == [current]
    assert _metadata(temp_db, result)["redirected_from"] == previous
