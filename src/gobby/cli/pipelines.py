"""
CLI commands for managing Gobby pipelines.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - facade for split pipeline modules
import getpass
import json
import logging
from pathlib import Path
from typing import Any

import click
import httpx
import yaml  # noqa: F401 - used by pipelines_import through this module facade

from gobby.cli._build_daemon import _daemon_error_detail, _daemon_error_message
from gobby.cli.pipelines_catalog import (
    check_pipeline,
    list_pipelines,
    run_pipeline,
    show_pipeline,
)
from gobby.cli.pipelines_import import import_pipeline
from gobby.cli.pipelines_runs import (
    approve_pipeline,
    history_pipeline,
    list_pipeline_runs,
    pipeline_runs,
    reject_pipeline,
    search_executions,
    show_pipeline_run,
)
from gobby.cli.runtime import require_cli_database
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.daemon_url import DaemonUrlError
from gobby.utils.json_helpers import json_dumps
from gobby.workflows.lobster_compat import (  # noqa: F401 - facade for pipelines_import
    LobsterImporter,
)
from gobby.workflows.pipeline_loader import PipelineLoader

logger = logging.getLogger(__name__)


def get_project_path() -> Path | None:
    """Get current project path if in a gobby project."""
    cwd = Path.cwd()
    if (cwd / ".gobby").exists():
        return cwd
    return None


def get_workflow_loader(db: HubDatabase | None = None) -> PipelineLoader:
    """Return a DB-backed pipeline loader."""
    return PipelineLoader(db=db or require_cli_database())


def _cli_actor() -> str:
    """Return the stable local actor recorded for CLI decisions."""
    return getpass.getuser()


def _get_project_id() -> str:
    """Get project ID from current project if available."""
    project_path = get_project_path()
    if not project_path:
        return ""
    project_json = project_path / ".gobby" / "project.json"
    if not project_json.exists():
        return ""
    try:
        with open(project_json) as f:
            project_data = json.load(f)
            project_id = project_data.get("id", "")
            return str(project_id) if project_id else ""
    except Exception:
        return ""


def get_pipeline_executor() -> Any:
    """Get pipeline executor instance for local CLI fallback.

    Creates a lightweight executor with template rendering support.
    MCP tool steps require the daemon; use _try_daemon_run() first.
    """
    from gobby.cli.runtime import require_cli_database
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.templates import TemplateEngine

    db = require_cli_database()

    project_id = _get_project_id()
    execution_manager = LocalPipelineExecutionManager(db, project_id)

    return PipelineExecutor(
        db=db,
        execution_manager=execution_manager,
        llm_service=None,  # Not needed for exec steps
        loader=get_workflow_loader(db),
        template_engine=TemplateEngine(),
    )


def _try_daemon_run(name: str, inputs: dict[str, str], project_id: str) -> dict[str, Any] | None:
    """Try to run pipeline via daemon HTTP API. Returns None only if unavailable."""
    try:
        from gobby.cli.utils_config import get_daemon_client

        client = get_daemon_client()
        response = client.call_http_api(
            "/api/pipelines/run",
            method="POST",
            json_data={"name": name, "inputs": inputs, "project_id": project_id},
            timeout=300.0,
        )
        if response.status_code in (200, 202):
            try:
                result: dict[str, Any] = response.json()
            except Exception as e:
                click.echo(f"Pipeline execution failed in daemon: invalid response: {e}", err=True)
                raise SystemExit(1) from None
            return result
        click.echo(
            "Pipeline execution failed in daemon: "
            f"{_daemon_error_message(_daemon_error_detail(response))}",
            err=True,
        )
        raise SystemExit(1)
    except (click.ClickException, DaemonUrlError, ValueError) as e:
        logger.debug("Daemon run unavailable for %s: %s", name, e, exc_info=True)
        return None
    except httpx.ReadTimeout:
        # The daemon accepted the run and is still executing it (long-running
        # pipelines wait on spawned agents). Falling back to the local
        # executor here would start a DUPLICATE execution.
        click.echo(
            f"Pipeline '{name}' is still running in the daemon "
            "(HTTP wait timed out). Check progress with: gobby pipelines runs list"
        )
        raise SystemExit(0) from None
    except (httpx.RequestError, ConnectionError, OSError) as e:
        logger.debug("Daemon run failed for %s: %s", name, e, exc_info=True)
        return None


def _try_daemon_approval(action: str, token: str) -> dict[str, Any] | None:
    """Try to approve/reject via daemon. Returns None only if daemon is unavailable."""
    try:
        from gobby.cli.utils_config import get_daemon_client

        client = get_daemon_client()
        is_healthy, health_error = client.check_health()
        if not is_healthy:
            logger.debug("Daemon %s skipped: %s", action, health_error)
            return None

        response = client.call_http_api(
            f"/api/pipelines/{action}/{token}",
            method="POST",
            timeout=300.0,
        )
        if response.status_code in (200, 202):
            try:
                result: dict[str, Any] = response.json()
            except Exception as e:
                click.echo(f"Pipeline {action} failed in daemon: invalid response: {e}", err=True)
                raise SystemExit(1) from None
            return result
        click.echo(
            f"Pipeline {action} failed in daemon: "
            f"{_daemon_error_message(_daemon_error_detail(response))}",
            err=True,
        )
        raise SystemExit(1)
    except (click.ClickException, DaemonUrlError, ValueError) as e:
        logger.debug("Daemon %s unavailable: %s", action, e, exc_info=True)
        return None
    except (httpx.RequestError, ConnectionError, OSError) as e:
        logger.debug("Daemon %s failed: %s", action, e, exc_info=True)
        return None


def _pipeline_result_dict(execution: Any) -> dict[str, Any]:
    return {
        "execution_id": execution.id,
        "pipeline_name": execution.pipeline_name,
        "status": execution.status.value,
    }


def _echo_approval_result(action: str, result: dict[str, Any], json_format: bool) -> None:
    if json_format:
        click.echo(json_dumps(result, indent=2))
        return

    icon = "✓" if action == "approve" else "✗"
    verb = "approved" if action == "approve" else "rejected"
    click.echo(f"{icon} Pipeline {verb}")
    click.echo(f"  Execution ID: {result.get('execution_id', '')}")
    click.echo(f"  Status: {result.get('status', '')}")


def parse_input(input_str: str) -> tuple[str, str]:
    """Parse a key=value input string."""
    if "=" not in input_str:
        raise click.BadParameter(f"Input must be in 'key=value' format: {input_str}")
    key, value = input_str.split("=", 1)
    return key.strip(), value.strip()


def get_execution_manager() -> Any:
    """Get pipeline execution manager instance."""
    from gobby.cli.runtime import require_cli_database
    from gobby.storage.pipelines import LocalPipelineExecutionManager

    db = require_cli_database()

    project_id = _get_project_id()
    return LocalPipelineExecutionManager(db, project_id)


@click.group()
def pipelines() -> None:
    """Manage Gobby pipelines."""


pipelines.add_command(list_pipelines)
pipelines.add_command(show_pipeline)
pipelines.add_command(check_pipeline)
pipelines.add_command(run_pipeline)
pipelines.add_command(pipeline_runs)
pipelines.add_command(approve_pipeline)
pipelines.add_command(reject_pipeline)
pipelines.add_command(history_pipeline)
pipelines.add_command(search_executions)
pipelines.add_command(import_pipeline)


__all__ = [
    "approve_pipeline",
    "check_pipeline",
    "history_pipeline",
    "import_pipeline",
    "list_pipeline_runs",
    "list_pipelines",
    "pipeline_runs",
    "pipelines",
    "reject_pipeline",
    "run_pipeline",
    "search_executions",
    "show_pipeline",
    "show_pipeline_run",
]
