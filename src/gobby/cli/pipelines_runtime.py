"""Runtime helpers shared by pipeline CLI command modules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import click
import httpx

from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)


def get_workflow_loader() -> WorkflowLoader:
    """Get workflow loader instance."""
    return WorkflowLoader()


def get_project_path() -> Path | None:
    """Get current project path if in a gobby project."""
    cwd = Path.cwd()
    if (cwd / ".gobby").exists():
        return cwd
    return None


def get_project_id() -> str:
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
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return ""


def _daemon_error_message(response: Any) -> str:
    status_code = getattr(response, "status_code", "unknown")
    fallback = str(getattr(response, "text", "") or "").strip() or f"HTTP {status_code}"
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return fallback

    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if detail:
            return json.dumps(detail)

    return fallback


def try_daemon_run(name: str, inputs: dict[str, str], project_id: str) -> dict[str, Any] | None:
    """Try to run pipeline via daemon HTTP API. Returns None only if unavailable."""
    try:
        from gobby.config.app import load_config
        from gobby.utils.daemon_client import DaemonClient

        config = load_config()
        client = DaemonClient(port=config.daemon_port)
        response = client.call_http_api(
            "/api/pipelines/run",
            method="POST",
            json_data={"name": name, "inputs": inputs, "project_id": project_id},
            timeout=300.0,
        )
        if response.status_code in (200, 202):
            try:
                result: dict[str, Any] = response.json()
            except (TypeError, ValueError) as e:
                click.echo(f"Pipeline execution failed in daemon: invalid response: {e}", err=True)
                raise SystemExit(1) from None
            return result
        click.echo(
            f"Pipeline execution failed in daemon: {_daemon_error_message(response)}",
            err=True,
        )
        raise SystemExit(1)
    except (httpx.RequestError, ConnectionError, OSError) as e:
        logger.debug("Daemon run failed for %s: %s", name, e, exc_info=True)
        return None


def get_pipeline_executor() -> Any:
    """Get pipeline executor instance for local CLI fallback."""
    from gobby.storage.hub.runtime import open_runtime_hub_database
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.templates import TemplateEngine

    db = open_runtime_hub_database(apply_migrations=False)

    project_id = get_project_id()
    execution_manager = LocalPipelineExecutionManager(db, project_id)

    return PipelineExecutor(
        db=db,
        execution_manager=execution_manager,
        llm_service=None,
        loader=get_workflow_loader(),
        template_engine=TemplateEngine(),
    )


def parse_input(input_str: str) -> tuple[str, str]:
    """Parse a key=value input string."""
    if "=" not in input_str:
        raise click.BadParameter(f"Input must be in 'key=value' format: {input_str}")
    key, value = input_str.split("=", 1)
    return key.strip(), value.strip()
