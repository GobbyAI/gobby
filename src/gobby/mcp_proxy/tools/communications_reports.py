"""Status-report composition and operator attachment tools."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.status_report.compose import compose_report
from gobby.status_report.delivery import ReportStore, deliver_artifact
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id, is_daemon_execution

logger = logging.getLogger(__name__)


def _report_refusal(file_path: str) -> dict[str, Any] | None:
    """Refuse a daemon-run attachment unless it resolves inside the project's reports directory."""
    project_context = get_project_context()
    project_path = project_context.get("project_path") if project_context else None
    if not isinstance(project_path, str) or not project_path:
        return {"success": False, "code": "forbidden", "error": "Project checkout is unknown"}
    if not Path(file_path).is_absolute():
        return {"success": False, "code": "forbidden", "error": "Report path must be absolute"}
    reports = Path(project_path) / ".gobby" / "reports"
    # resolve() collapses '..' and follows symlinks, so an escape cannot pass the containment test.
    target = Path(file_path).resolve()
    if target == reports.resolve() or not target.is_relative_to(reports.resolve()):
        return {
            "success": False,
            "code": "forbidden",
            "error": "Operator reports must be inside the project reports directory",
        }
    return None


def register_report_tools(
    registry: InternalToolRegistry,
    db: HubDatabase | None,
    operator_destination: Callable[[str, str | None], str],
    send_attachment: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    @registry.tool(
        description=(
            "Send an existing workspace file directly to the enrolled operator's Telegram DM. "
            "Requires an explicit operator_messaging grant in the installed agent definition. "
            "Takes no destination and preserves conversation attachment state."
        )
    )
    async def send_operator_attachment(
        channel: str,
        file_path: str,
        caption: str = "",
        filename: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        try:
            destination = await asyncio.to_thread(
                operator_destination, channel, get_current_session_id()
            )
            if is_daemon_execution() and (refusal := _report_refusal(file_path)) is not None:
                return refusal
            context = get_project_context() or {}
            store = (
                ReportStore(
                    db, str(context["id"]), require_machine_id(), json.dumps([channel, destination])
                )
                if is_daemon_execution() and db is not None and context.get("id")
                else None
            )

            async def send(delivery_path: Path) -> dict[str, Any]:
                return await send_attachment(
                    channel=channel,
                    file_path=str(delivery_path),
                    caption=caption,
                    filename=filename,
                    content_type=content_type,
                    metadata={"platform_destination": destination, "thread_id": None},
                )

            return await deliver_artifact(store, Path(file_path), send)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("Operator attachment delivery failed")
            return {"success": False, "error": str(exc)}

    @registry.tool(
        description=(
            "Compose the project's operator status report from hub evidence, git and live agent "
            "runs, and write it under the project's reports directory. Daemon-run steps only; "
            "deliver the returned file_path with send_operator_attachment."
        )
    )
    async def compose_status_report(
        channel: str,
        integration_branch: str,
        operator_timezone: str,
        window_seconds: int = 1800,
        load_threshold: float = 60.0,
    ) -> dict[str, Any]:
        if not is_daemon_execution():
            return {
                "success": False,
                "code": "forbidden",
                "error": "Status reports are composed by daemon-run steps only",
            }
        project_context = get_project_context()
        project_id = project_context.get("id") if project_context else None
        project_path = project_context.get("project_path") if project_context else None
        if db is None or not isinstance(project_id, str) or not isinstance(project_path, str):
            return {"success": False, "error": "Project checkout or storage is unavailable"}
        try:
            destination = await asyncio.to_thread(
                operator_destination, channel, get_current_session_id()
            )
            return await asyncio.to_thread(
                compose_report,
                db,
                project_id=project_id,
                project_path=Path(project_path),
                integration_branch=integration_branch,
                operator_timezone=operator_timezone,
                window=timedelta(seconds=window_seconds),
                load_threshold=load_threshold,
                now=utc_now(),
                destination_key=json.dumps([channel, destination]),
            )
        except Exception as exc:
            logger.exception("Status report composition failed")
            return {"success": False, "error": str(exc)}
