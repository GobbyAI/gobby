"""Daemon-run pipelines send operator attachments only under their declared grant."""

import json
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
from gobby.utils.session_context import reset_daemon_execution, set_daemon_execution

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


def _run_session(temp_db: HubDatabase, project_id: str, definition: dict[str, Any]) -> Any:
    """A cron-owned pipeline run: a session with no agent grant, linked to its definition."""
    session = SessionManager(temp_db).register(
        external_id=f"cron-run-{datetime.now(UTC).timestamp()}",
        machine_id=require_machine_id(),
        source="cron",
        project_id=project_id,
        agent_depth=0,
    )
    LocalPipelineExecutionManager(temp_db, project_id).create_execution(
        pipeline_name=SYNTHETIC_PIPELINE,
        session_id=session.id,
        definition_json=json.dumps({"name": SYNTHETIC_PIPELINE, **definition}),
        project_id=project_id,
    )
    return session


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "checkout"
    reports = project_root / ".gobby" / "reports"
    reports.mkdir(parents=True)
    return project_root, reports


async def _send(
    manager: MagicMock,
    temp_db: HubDatabase,
    caller_id: str,
    project_root: Path,
    channel: str,
    file_path: Path,
    *,
    daemon: bool,
) -> dict[str, Any]:
    tools = create_communications_registry(manager, db=temp_db, workspace_root=project_root)
    handler = tools.get_tool("send_operator_attachment")
    assert handler is not None
    token = set_daemon_execution() if daemon else None
    try:
        with (
            patch(
                "gobby.mcp_proxy.tools.communications.get_current_session_id",
                return_value=caller_id,
            ),
            patch(
                "gobby.mcp_proxy.tools.communications.get_project_context",
                return_value={"project_path": str(project_root)},
            ),
        ):
            result: dict[str, Any] = await handler(channel=channel, file_path=str(file_path))
        return result
    finally:
        if token is not None:
            reset_daemon_execution(token)


@pytest.mark.asyncio
async def test_granted_run_delivers_report_from_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    run = _run_session(temp_db, project_id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager, temp_db, run.id, project_root, "operator-status", report, daemon=True
    )

    assert result["success"] is True
    manager.send_attachment.assert_awaited_once_with(
        channel_name="operator-status",
        file_path=report.resolve(),
        filename=None,
        content_type="text/markdown",
        content="",
        session_id=run.id,
        metadata={"platform_destination": "42", "thread_id": None},
    )


@pytest.mark.asyncio
async def test_run_without_declared_grant_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    run = _run_session(temp_db, project_id, {"operator_messaging": False})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager, temp_db, run.id, project_root, "operator-status", report, daemon=True
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_granted_run_refused_on_unbound_channel(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    run = _run_session(temp_db, project_id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", "other-project")])
    project_root, reports = _checkout(tmp_path)
    report = reports / "gobby-digest-2026-11-04.md"
    report.write_text("# Digest\n")

    result = await _send(
        manager, temp_db, run.id, project_root, "operator-status", report, daemon=True
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_granted_run_refuses_path_outside_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    run = _run_session(temp_db, project_id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    outside = project_root / "secrets.md"
    outside.write_text("not a digest\n")
    traversal = reports / ".." / ".." / "secrets.md"

    result = await _send(
        manager, temp_db, run.id, project_root, "operator-status", traversal, daemon=True
    )

    assert result["success"] is False
    assert result["code"] == "forbidden"
    manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_granted_run_refuses_symlink_escaping_reports_directory(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    run = _run_session(temp_db, project_id, {"operator_messaging": True})
    manager = _manager([_channel("operator-status", project_id)])
    project_root, reports = _checkout(tmp_path)
    outside = project_root / "secrets.md"
    outside.write_text("not a digest\n")
    link = reports / "gobby-digest-2026-11-04.md"
    link.symlink_to(outside)

    result = await _send(
        manager, temp_db, run.id, project_root, "operator-status", link, daemon=True
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
        manager, temp_db, caller.id, project_root, "operator-status", report, daemon=False
    )

    assert result["success"] is False
    manager.send_attachment.assert_not_awaited()
