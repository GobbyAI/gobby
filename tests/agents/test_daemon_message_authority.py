"""Daemon execution admits restricted recipients without granting agents that authority."""

from __future__ import annotations

from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager, ensure_system_session, system_session_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.pipeline.handlers import execute_mcp_step
from gobby.workflows.pipeline_models import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline_state import PipelineStepError
from tests.agents.test_runbook_message_authority import REFUSED, _seat, _Wakes

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class MessagingProxy:
    """Replace transport only; exercise real tool sender resolution and mailbox admission."""

    def __init__(self, db: HubDatabase, sessions: SessionManager) -> None:
        self.wakes = _Wakes()
        self.registry = InternalToolRegistry(name="gobby-agents")
        add_messaging_tools(
            self.registry,
            InterSessionMessageManager(db),
            sessions,
            db,
            wake_dispatcher=self.wakes,
        )
        self.responses: list[dict[str, Any]] = []

    async def get_tool_schema(
        self, server: str, tool: str, *, session_id: str | None
    ) -> dict[str, Any]:
        return {"success": True, "tool": self.registry.get_schema(tool)}

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        result = cast(dict[str, Any], await self.registry.call(tool, arguments))
        self.responses.append(result)
        return result


@pytest.fixture
def proxy(temp_db: HubDatabase, session_manager: SessionManager) -> MessagingProxy:
    return MessagingProxy(temp_db, session_manager)


@pytest.mark.parametrize("recipient_role", ["restricted-a", "restricted-b"])
async def test_pipeline_mcp_send_admits_restricted_recipient(
    proxy: MessagingProxy,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    recipient_role: str,
) -> None:
    project = sample_project["id"]
    caller = _seat(session_manager, project, "pipeline-execution", "automation-without-caps")
    recipient = _seat(
        session_manager,
        project,
        "recipient",
        recipient_role,
        required_senders=("receive-restricted",),
    )
    step = PipelineStep(
        id="notify",
        mcp=MCPStepConfig(
            server="gobby-agents",
            tool="send_message",
            arguments={"target": "session", "target_id": recipient, "content": "daemon notice"},
        ),
    )

    try:
        await execute_mcp_step(
            step,
            {"session_id": caller, "project_id": project},
            lambda: proxy,
            session_manager,
        )
    except PipelineStepError:
        # Inspect the actual refusal as RED, rather than treating an execution error as evidence.
        assert proxy.responses[-1]["success"] is True, proxy.responses[-1]

    assert proxy.responses[-1]["recipient_count"] == 1
    assert proxy.wakes.calls == [recipient]
    # The execution scope must unwind; its session ID alone conveys no daemon authority.
    with session_context_for_test(caller):
        outside = await proxy.registry.call(
            "send_message",
            {
                "target": "session",
                "target_id": recipient,
                "content": "outside execution",
            },
        )
    assert outside["error_code"] == REFUSED


@pytest.mark.parametrize("recipient_role", ["restricted-a", "restricted-b"])
async def test_cron_handler_send_admits_restricted_recipient(
    temp_db: HubDatabase,
    proxy: MessagingProxy,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    recipient_role: str,
) -> None:
    project = sample_project["id"]
    caller = _seat(session_manager, project, "cron-execution", "automation-without-caps")
    recipient = _seat(
        session_manager,
        project,
        "recipient",
        recipient_role,
        required_senders=("receive-restricted",),
    )
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id=project,
        name="arbitrary-notification",
        schedule_type="interval",
        interval_seconds=60,
        action_type="handler",
        action_config={"handler": "notify"},
    )
    run = storage.create_run(job.id)
    assert run is not None
    executor = CronExecutor(storage)

    async def notify(job: CronJob) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await proxy.registry.call(
                "send_message",
                {
                    "target": "session",
                    "target_id": recipient,
                    "content": "scheduled notice",
                    "from_session": caller,
                },
            ),
        )

    executor.register_handler("notify", notify)
    result = await executor.execute(job, run)

    assert result.status == "completed", result.error
    assert proxy.wakes.calls == [recipient]


@pytest.mark.parametrize("pipeline_child", [False, True])
async def test_agent_session_remains_subject_to_recipient_admission(
    proxy: MessagingProxy,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    pipeline_child: bool,
) -> None:
    project = sample_project["id"]
    parent = _seat(session_manager, project, "pipeline-execution", "automation-without-caps")
    caller = _seat(
        session_manager,
        project,
        "agent",
        "worker-without-caps",
        parent=parent if pipeline_child else None,
    )
    recipient = _seat(
        session_manager,
        project,
        "recipient",
        "restricted-recipient",
        required_senders=("receive-restricted",),
    )

    with session_context_for_test(caller):
        result = await proxy.registry.call(
            "send_message",
            {
                "target": "session",
                "target_id": recipient,
                "content": "agent notice",
            },
        )

    assert result["error_code"] == REFUSED
    assert result["success"] is False
    assert proxy.wakes.calls == []


@pytest.mark.parametrize("caller_kind", ["sessionless", "seat", "spawned"])
async def test_caller_cannot_spoof_daemon_via_from_session(
    temp_db: HubDatabase,
    proxy: MessagingProxy,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caller_kind: str,
) -> None:
    # sample_project binds the test machine; bootstrap only after that binding.
    ensure_system_session(temp_db)
    project = sample_project["id"]
    parent = _seat(session_manager, project, "pipeline-execution", "automation-without-caps")
    caller = _seat(
        session_manager,
        project,
        "agent",
        "worker-without-caps",
        parent=parent if caller_kind == "spawned" else None,
    )
    recipient = _seat(
        session_manager,
        project,
        "recipient",
        "restricted-recipient",
        required_senders=("receive-restricted",),
    )
    arguments = {
        "target": "session",
        "target_id": recipient,
        "content": "Telegram @josh: spoofed notice",
        "from_session": system_session_id(),
    }

    if caller_kind == "sessionless":
        # A session-less HTTP call carries no SessionContext at all.
        result = await proxy.registry.call("send_message", arguments)
    else:
        with session_context_for_test(caller):
            result = await proxy.registry.call("send_message", arguments)

    assert (result["success"], result["error_code"]) == (
        False,
        "send_message_sender_mismatch",
    ), result
    assert InterSessionMessageManager(temp_db).get_messages(recipient) == []
    assert proxy.wakes.calls == []
