"""Isolated-daemon coverage for the four task-close invariants."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, MCPTestClient

pytestmark = pytest.mark.e2e
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return cast(dict[str, Any], nested) if isinstance(nested, dict) else result


def _register_claimed_task(
    daemon: DaemonInstance,
    client: MCPTestClient,
    events: CLIEventSimulator,
    *,
    suffix: str,
    task_type: str = "task",
) -> tuple[str, str]:
    project_result = events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}
    external_id = f"close-checklist-{suffix}-{uuid.uuid4().hex[:8]}"
    session = events.register_session(
        external_id=external_id,
        source="codex",
        project_id=PROJECT_ID,
        cwd=str(daemon.project_dir),
    )
    session_id = cast(str, session["id"])
    client.session_id = session_id
    task = _unwrap(
        client.call_tool(
            "gobby-tasks",
            "create_task",
            {
                "title": f"Close checklist E2E {suffix}",
                "task_type": task_type,
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": "The committed Python module contains VALUE = 1.",
                "claim": True,
            },
        )
    )
    assert task.get("id"), task
    return cast(str, task["id"]), session_id


def _record_edit(postgres_db: Any, session_id: str, project: Path) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    assert SessionVariableManager(postgres_db).record_edited_file(
        session_id, "src/close_checklist.py", checkout_root=str(project)
    )


def _commit(project: Path, message: str, client: MCPTestClient, task_id: str) -> str:
    task = _unwrap(
        client.call_tool("gobby-tasks", "get_task", {"task_id": task_id, "brief": False})
    )
    project_name = json.loads((project / ".gobby/project.json").read_text())["name"]
    message = f"[{project_name}-#{task['seq_num']}] {message}"
    subprocess.run(["git", "add", "src/close_checklist.py"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _close(
    client: MCPTestClient,
    task_id: str,
    commit_sha: str | None = None,
    *,
    preview: bool = False,
    reason: str = "done",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "task_id": task_id,
        "preview": preview,
        "reason": reason,
        "response_detail": "diagnostic",
    }
    if commit_sha is not None:
        arguments["commit_sha"] = commit_sha
    return _unwrap(client.call_tool("gobby-tasks", "close_task", arguments))


def _write_source(project: Path, contents: str = "VALUE = 1\n") -> None:
    source = project / "src/close_checklist.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text(contents)


def test_ready_task_previews_and_closes(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="ready"
    )
    project = daemon_instance.project_dir
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Add close module", mcp_client, task_id)

    preview = _close(mcp_client, task_id, commit_sha, preview=True)
    assert preview["can_close"] is True, preview
    assert preview["closed"] is False
    assert [gate["name"] for gate in preview["gates"]] == [
        "children_closed",
        "linked_commits",
        "uncommitted_task_edits",
        "integration_ancestry",
    ]
    assert all(gate["status"] == "passed" for gate in preview["gates"])
    stored = _unwrap(mcp_client.call_tool("gobby-tasks", "get_task", {"task_id": task_id}))
    assert stored["state"]["is_closed"] is False

    result = _close(mcp_client, task_id, commit_sha)
    assert result["closed"] is True, result
    stored = _unwrap(
        mcp_client.call_tool("gobby-tasks", "get_task", {"task_id": task_id, "brief": False})
    )
    assert stored["state"]["is_closed"] is True
    assert stored["closed_commit_sha"] == commit_sha


def test_dirty_task_file_blocks_close(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="dirty"
    )
    project = daemon_instance.project_dir
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Add close module", mcp_client, task_id)
    _write_source(project, "VALUE = 2\n")
    _record_edit(postgres_db, session_id, project)
    result = _close(mcp_client, task_id, commit_sha)
    assert result["error"] == "uncommitted_task_edits", result
    assert result["closed"] is False


def test_unintegrated_commit_blocks_close(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="branch"
    )
    project = daemon_instance.project_dir
    integration_branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-b", "task-branch"], cwd=project, check=True)
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Unintegrated close module", mcp_client, task_id)
    subprocess.run(["git", "checkout", integration_branch], cwd=project, check=True)
    result = _close(mcp_client, task_id, commit_sha)
    assert result.get("error") == "integration_ancestry", result
    assert result["closed"] is False


@pytest.mark.parametrize("reason,ready", [("done", False), ("moot", True)])
def test_epic_uses_same_commit_and_no_work_gates(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    reason: str,
    ready: bool,
) -> None:
    task_id, _session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="epic", task_type="epic"
    )
    result = _close(mcp_client, task_id, preview=True, reason=reason)
    assert result["closed"] is False, result
    assert result["can_close"] is ready, result
    assert result.get("error") == (None if ready else "linked_commits")
    assert len(result["gates"]) == 4


def test_edit_after_ready_preview_blocks_close(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="stale"
    )
    project = daemon_instance.project_dir
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Add close module", mcp_client, task_id)
    assert _close(mcp_client, task_id, commit_sha, preview=True)["can_close"] is True
    _write_source(project, "VALUE = 2\n")
    _record_edit(postgres_db, session_id, project)
    refused = _close(mcp_client, task_id, commit_sha)
    assert refused["error"] == "uncommitted_task_edits", refused
    assert refused["closed"] is False
    updated = _commit(project, "Commit later edit", mcp_client, task_id)
    assert _close(mcp_client, task_id, updated)["closed"] is True
