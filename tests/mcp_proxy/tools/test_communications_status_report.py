"""Daemon-run steps compose the status report under the project's reports directory."""

import json
import uuid
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

pytestmark = pytest.mark.integration

SYNTHETIC_PIPELINE = "synthetic-status-run"
OPERATOR_CHANNEL = "operator-status"
COMPOSE_ARGUMENTS = {
    "channel": OPERATOR_CHANNEL,
    "integration_branch": "0.5.0",
    "operator_timezone": "America/Chicago",
    "window_seconds": 1800,
    # Far above any real load so the test never depends on the machine it runs on.
    "load_threshold": 10_000.0,
}


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
    message = MagicMock(status="sent", id=str(uuid.uuid4()), error=None, platform_message_id="p1")
    attachment = MagicMock(
        id="att",
        message_id="delivered",
        filename="status-report.md",
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


async def _run_step(
    manager: MagicMock,
    temp_db: HubDatabase,
    project_id: str,
    session_id: str,
    project_root: Path,
    execution_id: str,
    *,
    step_id: str,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run one communications tool as a daemon-run pipeline step and return its output."""
    tools = create_communications_registry(manager, db=temp_db, workspace_root=project_root)
    handler = tools.get_tool(tool)
    assert handler is not None
    proxy = _CommsProxy(handler)
    step = PipelineStep(
        id=step_id,
        mcp=MCPStepConfig(server="gobby-communications", tool=tool, arguments=arguments),
    )
    context: dict[str, Any] = {
        "session_id": session_id,
        "project_id": project_id,
        "_pipeline_execution_id": execution_id,
    }
    with (
        patch(
            "gobby.mcp_proxy.tools.communications.get_current_session_id",
            return_value=session_id,
        ),
        patch(
            "gobby.mcp_proxy.tools.communications.get_project_context",
            return_value={"id": project_id, "project_path": str(project_root)},
        ),
        patch(
            "gobby.mcp_proxy.tools.communications_reports.get_current_session_id",
            return_value=session_id,
        ),
        patch(
            "gobby.mcp_proxy.tools.communications_reports.get_project_context",
            return_value={"id": project_id, "project_path": str(project_root)},
        ),
    ):
        output: dict[str, Any] = await execute_mcp_step(
            step, context, lambda: proxy, SessionManager(temp_db)
        )
    return output


@pytest.mark.asyncio
async def test_compose_is_refused_outside_a_daemon_run(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    project_root = tmp_path / "checkout"
    project_root.mkdir()
    tools = create_communications_registry(_manager([]), db=temp_db, workspace_root=project_root)
    handler = tools.get_tool("compose_status_report")
    assert handler is not None

    with patch(
        "gobby.mcp_proxy.tools.communications.get_project_context",
        return_value={"id": project_id, "project_path": str(project_root)},
    ):
        result = await handler(**COMPOSE_ARGUMENTS)

    assert result["success"] is False
    assert result["code"] == "forbidden"
    assert not (project_root / ".gobby" / "reports" / "status-report.md").exists()


@pytest.mark.asyncio
async def test_daemon_run_composes_report_under_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    project_root = tmp_path / "checkout"
    project_root.mkdir()

    output = await _run_step(
        _manager([_channel(OPERATOR_CHANNEL, project_id)]),
        temp_db,
        project_id,
        session.id,
        project_root,
        run,
        step_id="compose",
        tool="compose_status_report",
        arguments=COMPOSE_ARGUMENTS,
    )

    report = Path(output["file_path"])
    assert report.parent == project_root / ".gobby" / "reports"
    assert report.name.startswith("status-report-")
    assert output["nominal"] is True
    assert output["mode"] == "full"
    assert report.read_text(encoding="utf-8").endswith("Systems nominal\n")


@pytest.mark.asyncio
async def test_composed_report_is_delivered_by_the_next_daemon_step(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {"operator_messaging": True})
    project_root = tmp_path / "checkout"
    project_root.mkdir()
    manager = _manager([_channel(OPERATOR_CHANNEL, project_id)])

    composed = await _run_step(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        run,
        step_id="compose",
        tool="compose_status_report",
        arguments=COMPOSE_ARGUMENTS,
    )
    original = Path(composed["file_path"]).resolve()
    expected_bytes = original.read_bytes()
    observed: list[bytes] = []
    response = manager.send_attachment.return_value

    async def send_snapshot(**kwargs: Any) -> Any:
        observed.append(kwargs["file_path"].read_bytes())
        return response

    manager.send_attachment.side_effect = send_snapshot
    delivered = await _run_step(
        manager,
        temp_db,
        project_id,
        session.id,
        project_root,
        run,
        step_id="deliver",
        tool="send_operator_attachment",
        arguments={"channel": OPERATOR_CHANNEL, "file_path": composed["file_path"]},
    )

    assert "error" not in delivered
    manager.send_attachment.assert_awaited_once()
    sent = manager.send_attachment.await_args.kwargs
    assert sent["channel_name"] == OPERATOR_CHANNEL
    assert sent["file_path"] != original
    assert sent["file_path"].name == original.name
    assert observed == [expected_bytes]
    assert not sent["file_path"].exists()
    receipt = temp_db.fetchone(
        "SELECT delivered_at, message_id FROM status_report_runs WHERE id = %s",
        (composed["run_id"],),
    )
    assert receipt is not None and receipt["delivered_at"] is not None
    assert str(receipt["message_id"]) == delivered["message"]["id"]


@pytest.mark.asyncio
async def test_compose_step_failure_stops_the_run(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    session = _session(temp_db, project_id)
    run = _execution(temp_db, project_id, session.id, {})
    project_root = tmp_path / "checkout"
    project_root.mkdir()

    with pytest.raises(PipelineStepError):
        await _run_step(
            _manager([]),
            temp_db,
            project_id,
            session.id,
            project_root,
            run,
            step_id="compose",
            tool="compose_status_report",
            arguments={**COMPOSE_ARGUMENTS, "operator_timezone": "Not/A_Zone"},
        )
    assert not (project_root / ".gobby" / "reports" / "status-report.md").exists()
