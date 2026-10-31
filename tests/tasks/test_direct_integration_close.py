"""Capability-scoped commits select their declared close evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.step_records import StepSubmission, record_task_step
from gobby.utils.session_context import session_context_for_test
from tests.tasks.test_close_invariants import git
from tests.tasks.test_step_records import _Seats

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("capability", "paths", "earlier_code", "can_close"),
    [
        (True, ["policy/custom.yaml"], False, True),
        (False, ["policy/custom.yaml"], False, False),
        (True, ["policy/custom.yaml", "src/work.py"], False, False),
        (True, ["policy/custom.yaml"], True, False),
    ],
)
async def test_close_selects_steps_from_declared_paths(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    capability: bool,
    paths: list[str],
    earlier_code: bool,
    can_close: bool,
) -> None:
    repo = Path(sample_git_project["repo_path"])
    marker = repo / ".gobby" / "project.json"
    policy = json.loads(marker.read_text())
    policy["task_close_policy"] = {"enforce_step_records": True}
    marker.write_text(json.dumps(policy))
    git(repo, "add", ".gobby/project.json")
    git(repo, "commit", "-m", "enable evidence policy")

    seat = _Seats(temp_db, sample_git_project["id"]).add(
        "custom-policy-maintainer",
        ["closing"],
        capabilities=("direct_integration_commit",) if capability else (),
        definition_fields={
            "direct_integration_commit_paths": ["policy/*.yaml"],
            "direct_integration_commit_steps": ["closing"],
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_git_project["id"],
        "Update declared configuration",
        category="code",
        validation_criteria="Declared paths select closing-only evidence.",
    )
    manager.claim_task(task.id, seat)
    if earlier_code:
        (repo / "earlier.py").write_text("value = 1\n")
        git(repo, "add", "earlier.py")
        git(repo, "commit", "-m", "earlier code change")
        manager.link_commit(task.id, git(repo, "rev-parse", "HEAD"))
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value: changed\n")
    git(repo, "add", "--", *paths)
    git(repo, "commit", "-m", "configuration change")
    sha = git(repo, "rev-parse", "HEAD")
    manager.link_commit(task.id, sha)
    record_task_step(
        temp_db,
        task_id=task.id,
        session_id=seat,
        step="closing",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=1,
            commit_sha=sha,
            feedback="Configuration change is landed and checked.",
        ),
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, RegistryContext(task_manager=manager))

    with session_context_for_test(seat):
        preview = await registry.call(
            "close_task", {"task_id": task.id, "preview": True, "response_detail": "diagnostic"}
        )
        assert preview["can_close"] is can_close, preview
        step_gate = next(gate for gate in preview["gates"] if gate["name"] == "step_records")
        if can_close:
            assert step_gate["details"]["required_steps"] == ["closing"]
            result = await registry.call("close_task", {"task_id": task.id})
            assert result["closed"] is True, result
            assert manager.get_task(task.id).closed_commit_sha == sha
        else:
            assert set(step_gate["details"]["missing_steps"]) == {"implementation", "review"}
            assert manager.get_task(task.id).closed_at is None
