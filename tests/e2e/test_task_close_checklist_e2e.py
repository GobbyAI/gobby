"""Isolated-daemon coverage for the five task-close gates."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.agents import sync as agent_sync
from gobby.agents.sync import sync_bundled_agents
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_paths, sync_rule_file
from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, MCPTestClient

pytestmark = pytest.mark.e2e
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
CLOSURE_GROUP_TAGS = frozenset({"task-closure-coding", "task-closure-general"})


@pytest.fixture(autouse=True)
def _installed_closure_definitions(
    postgres_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install the bundled closure rules and default agent through the production sync.

    The isolated daemon's project is not a dev checkout, so it never syncs bundled
    content itself. The rows are written here, into the schema the daemon reads.
    Rule files are selected by their bundled tags. The default agent comes from a
    byte copy of its bundled template.
    """
    for root in get_bundled_rules_paths():
        for rule_file in sorted(root.rglob("*.yaml")):
            if "deprecated" in rule_file.relative_to(root).parts:
                continue
            body = yaml.safe_load(rule_file.read_text(encoding="utf-8")) or {}
            if CLOSURE_GROUP_TAGS & set(body.get("tags") or []):
                synced = sync_rule_file(postgres_db, rule_file, tag="gobby")
                assert synced["success"], synced
    agents = tmp_path / "agents"
    agents.mkdir()
    shutil.copy2(agent_sync.get_bundled_agents_path() / "default.yaml", agents / "default.yaml")
    monkeypatch.setattr(agent_sync, "get_bundled_agents_path", lambda: agents)
    synced_agents = sync_bundled_agents(postgres_db)
    assert synced_agents["success"], synced_agents


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return cast(dict[str, Any], nested) if isinstance(nested, dict) else result


def _register_claimed_task(
    daemon: DaemonInstance,
    client: MCPTestClient,
    events: CLIEventSimulator,
    postgres_db: Any,
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
    # An unseated session has no _agent_type until activation. Bind it to the default
    # definition, as the activation fallback does, so attestations resolve an installed row.
    SessionVariableManager(postgres_db).set_variable(session_id, "_agent_type", "default")
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
    superseded_by: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "task_id": task_id,
        "preview": preview,
        "reason": reason,
        "response_detail": "diagnostic",
    }
    if commit_sha is not None:
        arguments["commit_sha"] = commit_sha
    if superseded_by is not None:
        arguments["superseded_by"] = superseded_by
    return _unwrap(client.call_tool("gobby-tasks", "close_task", arguments))


def _record_closure_evidence(client: MCPTestClient, task_id: str, commit_sha: str) -> None:
    """Record the approved attestations the coding closure rules need for one candidate."""
    criteria = [
        {
            "criterion": "The committed Python module contains VALUE = 1.",
            "met": True,
            "how": "src/close_checklist.py contains VALUE = 1",
        }
    ]
    steps: tuple[tuple[str, dict[str, Any], str | None], ...] = (
        ("test-before", {}, None),
        ("implementation", {}, commit_sha),
        ("test-after", {}, commit_sha),
        ("validation", {"stage": "implement", "criteria": criteria}, commit_sha),
        ("self-review", {}, commit_sha),
        ("closing", {}, commit_sha),
    )
    for kind, evidence, reference in steps:
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "kind": kind,
            "result": "approved",
            "summary": f"{kind} for {commit_sha}",
            "evidence": evidence,
        }
        if reference is not None:
            arguments["reference"] = {"scheme": "git", "value": reference}
        recorded = _unwrap(client.call_tool("gobby-tasks", "record_attestation", arguments))
        assert "error" not in recorded, recorded


def _record_disposition(
    client: MCPTestClient, task_id: str, reason: str, replacement: str | None = None
) -> None:
    """Record the approved disposition a non-done close needs, naming its replacement if any."""
    arguments: dict[str, Any] = {
        "task_id": task_id,
        "kind": "disposition",
        "result": "approved",
        "summary": f"closed as {reason}",
        "evidence": {"reason": reason},
    }
    if replacement is not None:
        arguments["reference"] = {"scheme": "task", "value": replacement}
    recorded = _unwrap(client.call_tool("gobby-tasks", "record_attestation", arguments))
    assert "error" not in recorded, recorded


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
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="ready"
    )
    project = daemon_instance.project_dir
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Add close module", mcp_client, task_id)
    _record_closure_evidence(mcp_client, task_id, commit_sha)

    preview = _close(mcp_client, task_id, commit_sha, preview=True)
    assert preview["can_close"] is True, preview
    assert preview["closed"] is False
    assert preview["blocking_reasons"] == [], preview
    assert preview["facts"]["task_files_clean"] is True, preview
    assert preview["facts"]["integration_ancestry"] is True, preview
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
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="dirty"
    )
    project = daemon_instance.project_dir
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Add close module", mcp_client, task_id)
    _write_source(project, "VALUE = 2\n")
    _record_edit(postgres_db, session_id, project)
    result = _close(mcp_client, task_id, commit_sha)
    assert result["error"] == "closure_policy", result
    assert result["blocking_reasons"], result
    assert result["facts"]["task_files_clean"] is False, result
    assert result["closed"] is False


def test_unintegrated_commit_blocks_close(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="branch"
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
    _record_closure_evidence(mcp_client, task_id, commit_sha)
    result = _close(mcp_client, task_id, commit_sha)
    assert result.get("error") == "closure_policy", result
    assert result["blocking_reasons"], result
    assert result["facts"]["integration_ancestry"] is False, result
    assert result["closed"] is False


@pytest.mark.parametrize("reason,ready", [("done", False), ("moot", True)])
def test_epic_uses_same_commit_and_no_work_gates(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
    reason: str,
    ready: bool,
) -> None:
    task_id, _session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="epic", task_type="epic"
    )
    if reason != "done":
        _record_disposition(mcp_client, task_id, reason)
    result = _close(mcp_client, task_id, preview=True, reason=reason)
    assert result["closed"] is False, result
    assert result["can_close"] is ready, result
    if ready:
        assert "error" not in result, result
        assert result["blocking_reasons"] == [], result
    else:
        assert result["error"] == "closure_policy", result
        assert result["blocking_reasons"], result


def test_edit_after_ready_preview_blocks_close(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="stale"
    )
    project = daemon_instance.project_dir
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    commit_sha = _commit(project, "Add close module", mcp_client, task_id)
    _record_closure_evidence(mcp_client, task_id, commit_sha)
    assert _close(mcp_client, task_id, commit_sha, preview=True)["can_close"] is True
    _write_source(project, "VALUE = 2\n")
    _record_edit(postgres_db, session_id, project)
    refused = _close(mcp_client, task_id, commit_sha)
    assert refused["error"] == "closure_policy", refused
    assert refused["facts"]["task_files_clean"] is False, refused
    assert refused["closed"] is False
    updated = _commit(project, "Commit later edit", mcp_client, task_id)
    _record_closure_evidence(mcp_client, task_id, updated)
    assert _close(mcp_client, task_id, updated)["closed"] is True


def _current_branch(project: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _link(client: MCPTestClient, task_id: str, commit_sha: str) -> None:
    linked = _unwrap(
        client.call_tool(
            "gobby-tasks", "link_commit", {"task_id": task_id, "commit_sha": commit_sha}
        )
    )
    assert "error" not in linked, linked


def _landed_and_candidate(
    daemon: DaemonInstance,
    client: MCPTestClient,
    events: CLIEventSimulator,
    postgres_db: Any,
    *,
    suffix: str,
) -> tuple[str, str, str, str]:
    """Link one commit on the integration branch and one unlanded task-branch commit."""
    task_id, session_id = _register_claimed_task(daemon, client, events, postgres_db, suffix=suffix)
    project = daemon.project_dir
    integration_branch = _current_branch(project)
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    landed = _commit(project, "Land close module", client, task_id)
    _link(client, task_id, landed)
    subprocess.run(["git", "checkout", "-b", "task-branch"], cwd=project, check=True)
    _write_source(project, "VALUE = 2\n")
    _record_edit(postgres_db, session_id, project)
    candidate = _commit(project, "Candidate close module", client, task_id)
    _link(client, task_id, candidate)
    subprocess.run(["git", "checkout", integration_branch], cwd=project, check=True)
    return task_id, landed, candidate, integration_branch


def test_superseded_task_closes_with_an_unlanded_candidate(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, session_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="superseded"
    )
    project = daemon_instance.project_dir
    integration_branch = _current_branch(project)
    subprocess.run(["git", "checkout", "-b", "task-branch"], cwd=project, check=True)
    _write_source(project)
    _record_edit(postgres_db, session_id, project)
    candidate = _commit(project, "Candidate close module", mcp_client, task_id)
    subprocess.run(["git", "checkout", integration_branch], cwd=project, check=True)

    _link(mcp_client, task_id, candidate)
    unlinked = _unwrap(
        mcp_client.call_tool(
            "gobby-tasks",
            "unlink_commit",
            {"task_id": task_id, "commit_sha": candidate, "reason": "Superseded by #24970"},
        )
    )
    assert "error" not in unlinked, unlinked
    _record_disposition(mcp_client, task_id, "superseded", "#24970")
    result = _close(mcp_client, task_id, reason="superseded", superseded_by="#24970")
    assert result["closed"] is True, result
    stored = _unwrap(
        mcp_client.call_tool("gobby-tasks", "get_task", {"task_id": task_id, "brief": False})
    )
    assert stored["state"]["is_closed"] is True
    assert stored["closed_reason"] == "superseded"
    closing_records = postgres_db.fetchall(
        "SELECT reason FROM task_lifecycle_events WHERE task_id = %s AND to_state = 'closed'",
        (task_id,),
    )
    assert [record["reason"] for record in closing_records] == [
        "closed as superseded; superseded by #24970"
    ]


def test_superseded_close_without_a_disposition_is_refused(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    """Precondition: the task has no approved disposition attestation.

    The superseded close is refused by the disposition rule. The landed-commit fact
    does not apply here, so this test asserts the disposition blocker, not ancestry.
    """
    task_id, _, candidate, _ = _landed_and_candidate(
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="superseded-mixed"
    )
    refused = _close(mcp_client, task_id, candidate, reason="superseded", superseded_by="#24970")
    blocker = (
        "Record an approved disposition attestation with evidence.reason matching the close reason."
    )
    assert refused["closed"] is False, refused
    assert refused["error"] == "closure_policy", refused
    assert blocker in refused["blocking_reasons"], refused
    assert refused["closed"] is False


def test_landed_commit_cannot_be_unlinked_from_a_claimed_task(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
) -> None:
    task_id, landed, candidate, _ = _landed_and_candidate(
        daemon_instance, mcp_client, cli_events, postgres_db, suffix="landed-unlink"
    )
    control = _unwrap(
        mcp_client.call_tool(
            "gobby-tasks",
            "unlink_commit",
            {"task_id": task_id, "commit_sha": candidate, "reason": "Superseded by #24970"},
        )
    )
    assert "error" not in control, control
    refused = _unwrap(
        mcp_client.call_tool(
            "gobby-tasks",
            "unlink_commit",
            {"task_id": task_id, "commit_sha": landed, "reason": "Superseded by #24970"},
        )
    )
    assert refused.get("error"), refused
    stored = _unwrap(
        mcp_client.call_tool("gobby-tasks", "get_task", {"task_id": task_id, "brief": False})
    )
    # link_commit stores Git's canonical short SHA, so match the stored entry as a prefix.
    assert any(landed.startswith(sha) for sha in stored.get("commits") or [])
