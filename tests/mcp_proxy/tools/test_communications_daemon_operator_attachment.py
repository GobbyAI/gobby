"""Daemon-run pipeline steps send operator attachments only under their own execution's grant."""

import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.models import ChannelConfig
from gobby.mcp_proxy.tools.communications import create_communications_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.pipeline.handlers import execute_mcp_step
from gobby.workflows.pipeline_models import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline_state import PipelineStepError

pytestmark = pytest.mark.unit

SYNTHETIC_PIPELINE = "synthetic-operator-report"


def _channel(name: str, operator_project_id: str) -> ChannelConfig:
    return ChannelConfig(
        id=f"channel-{name}",
        channel_type="telegram",
        name=name,
        enabled=True,
        config_json={
            "operator_project_id": operator_project_id,
            "responder": {"project_id": "chat-project"},
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _manager(channels: list[ChannelConfig]) -> MagicMock:
    manager = MagicMock()
    manager.list_channels.return_value = channels
    manager.resolve_operator_conversation.return_value = {
        "conversation_id": "dm:42",
        "session_id": None,
    }
    message = MagicMock(status="sent", id="delivered", error=None, platform_message_id="p1")
    attachment = MagicMock(
        id="att",
        message_id="delivered",
        filename="digest.md",
        content_type="text/markdown",
        size_bytes=1,
        platform_url=None,
    )
    manager.send_attachment = AsyncMock(return_value=(message, attachment))
    return manager


def _session(temp_db: HubDatabase, project_id: str) -> Any:
    """A cron-owned session with no agent grant; pipeline runs share it."""
    return SessionManager(temp_db).register(
        external_id=f"cron-run-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        source="cron",
        project_id=project_id,
        agent_depth=0,
    )


def _execution(
    temp_db: HubDatabase, project_id: str, session_id: str, definition: dict[str, Any]
) -> str:
    """One pipeline run on ``session_id``, carrying its definition snapshot."""
    execution = LocalPipelineExecutionManager(temp_db, project_id).create_execution(
        pipeline_name=SYNTHETIC_PIPELINE,
        session_id=session_id,
        definition_json=json.dumps({"name": SYNTHETIC_PIPELINE, **definition}),
        project_id=project_id,
    )
    return str(execution.id)


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "checkout"
    reports = project_root / ".gobby" / "reports"
    reports.mkdir(parents=True)
    return project_root, reports


class _CommsProxy:
    """Transport stand-in: a pipeline MCP step runs the real communications handler."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.last: dict[str, Any] | None = None

    async def get_tool_schema(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
        enforce_workflow: bool = False,
    ) -> dict[str, Any]:
        self.last = await self.handler(**arguments)
        return self.last


async def _send(
    manager: MagicMock,
    temp_db: HubDatabase,
    project_id: str,
    session_id: str,
    project_root: Path,
    channel: str,
    file_path: Path,
    *,
    execution_id: str | None = None,
    daemon: bool = True,
) -> dict[str, Any]:
    """Send one attachment as a pipeline step bound to ``execution_id``, or as an agent call."""
    tools = create_communications_registry(manager, db=temp_db, workspace_root=project_root)
    handler = tools.get_tool("send_operator_attachment")
    assert handler is not None
    with (
        patch(
            "gobby.mcp_proxy.tools.communications.get_current_session_id",
            return_value=session_id,
        ),
        patch(
            "gobby.mcp_proxy.tools.communications.get_project_context",
            return_value={"project_path": str(project_root)},
        ),
    ):
        if not daemon:
            result: dict[str, Any] = await handler(channel=channel, file_path=str(file_path))
            return result
        proxy = _CommsProxy(handler)
        step = PipelineStep(
            id="deliver",
            mcp=MCPStepConfig(
                server="gobby-communications",
                tool="send_operator_attachment",
                arguments={"channel": channel, "file_path": str(file_path)},
            ),
        )
        context: dict[str, Any] = {
            "session_id": session_id,
            "project_id": project_id,
            "_pipeline_execution_id": execution_id,
        }
        # A refusal surfaces as a step error; the handler's own result carries the reason.
        with suppress(PipelineStepError):
            await execute_mcp_step(step, context, lambda: proxy, SessionManager(temp_db))
        assert proxy.last is not None
        return proxy.last


@pytest.mark.asyncio
async def test_granted_run_delivers_report_from_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=run,
    )

    assert result["success"] is True
    manager.send_attachment.assert_awaited_once_with(
        channel_name="operator-status",
        file_path=report.resolve(),
        filename=None,
        content_type="text/markdown",
        content="",
        session_id=session.id,
        metadata={"platform_destination": "42", "thread_id": None},
    )


@pytest.mark.asyncio
async def test_run_without_declared_grant_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": False})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=run,
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_granted_run_refused_on_unbound_channel(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", "other-project")])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=run,
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_granted_run_refuses_path_outside_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    outside = project_root / "secrets.md"
    outside.write_text("not a digest\n")
    traversal = reports / ".." / ".." / "secrets.md"

    result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        traversal,
        execution_id=run,
    )

    assert result["success"] is False
    assert result["code"] == "forbidden"
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_granted_run_refuses_symlink_escaping_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    outside = project_root / "secrets.md"
    outside.write_text("not a digest\n")
    link = reports / "gobby-digest-2026-11-04.md"
    link.symlink_to(outside)

    result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        link,
        execution_id=run,
    )

    assert result["success"] is False
    assert result["code"] == "forbidden"
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_caller_without_grant_still_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    caller = SessionManager(temp_db).register(
        external_id="ordinary-cron-caller",
        machine_id=require_machine_id(),
        source="cron",
        project_id=project_id,
        agent_depth=0,
    )
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager,
        temp_db,
        project_id,
        caller.id,
        project_root,
        "operator-status",
        report,
        daemon=False,
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_ungranted_parent_gains_no_grant_from_granted_child(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    """Parent and child share one session; the child's grant must not outlive its step."""
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    parent = _execution(temp_db, project_id, session.id, {"operator_messaging": False})
    child = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    child_result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=child,
    )
    parent_result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=parent,
    )

    assert child_result["success"] is True
    assert parent_result["success"] is False
    manager.send_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_granted_parent_is_not_revoked_by_ungranted_child(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    """The child's refusal must not depend on the parent's newer grant row in the same session."""
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    child = _execution(temp_db, project_id, session.id, {"operator_messaging": False})
    parent = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    child_result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=child,
    )
    parent_result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=parent,
    )

    assert child_result["success"] is False
    assert parent_result["success"] is True
    manager.send_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_step_without_bound_execution_gets_no_session_grant(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    """A granted run in the session does not authorize a step that names no execution."""
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        "operator-status",
        report,
        execution_id=None,
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()
