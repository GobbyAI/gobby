"""Installed definitions drive self-completion and independent review without Git."""

from pathlib import Path

import pytest
import yaml

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import list_attestations
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_rule_file
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.e2e
_WORKFLOWS = Path(__file__).parents[2] / "src/gobby/install/shared/workflows"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paired", [False, True], ids=["default-self-completes", "independent-pair"]
)
@pytest.mark.parametrize("checkout_available", [False, True], ids=["no-checkout", "directory"])
@pytest.mark.parametrize("incomplete_attribution", [False, True], ids=["empty", "unresolved-edits"])
async def test_non_git_task_uses_installed_completion_policy(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paired: bool,
    checkout_available: bool,
    incomplete_attribution: bool,
) -> None:
    project = install_isolated_checkout_project(
        temp_db, tmp_path / "recipe-project", monkeypatch=monkeypatch
    )
    root = Path(project.root_path)
    if not checkout_available:
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.resolve_task_repo_path",
            lambda **kwargs: None,
        )
    for path in sorted((_WORKFLOWS / "rules/task-closure").glob("*.yaml")):
        synced = sync_rule_file(temp_db, path)
        assert synced["success"], synced["errors"]
    maker_name = "developer" if paired else "default"
    definitions = AgentDefinitionManager(temp_db)
    for name in {maker_name, "code-reviewer"}:
        body = yaml.safe_load((_WORKFLOWS / f"agents/{name}.yaml").read_text())
        definitions.create(name, body, project_id=project.project.id)
    sessions = SessionManager(temp_db)
    maker = sessions.register(
        external_id="recipe-maker",
        source="codex",
        machine_id=project.machine_id,
        project_id=project.project.id,
    )
    reviewer = sessions.register(
        external_id="recipe-reviewer",
        source="codex",
        machine_id=project.machine_id,
        project_id=project.project.id,
    )
    variables = SessionVariableManager(temp_db)
    variables.set_variable(maker.id, "_agent_type", maker_name)
    variables.set_variable(reviewer.id, "_agent_type", "code-reviewer")
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project.project.id,
        "Write the cake recipe",
        category="manual",
        validation_criteria="The recipe lists ingredients and a baking temperature.",
        created_in_session_id=maker.id,
    )
    registry = create_task_registry(tasks)
    closer = maker

    with session_context_for_test(maker.id):
        claim = await registry.call("claim_task", {"task_id": task.id})
    assert claim["success"] is True, claim
    assert tasks.get_task(task.id).claimed_by_session_id == maker.id
    artifact = root / "cake.txt"
    artifact.write_text("Ingredients: flour, butter, eggs. Bake at 180 C.\n")

    with session_context_for_test(closer.id):
        missing = await registry.call("close_task", {"task_id": task.id, "preview": True})
    assert missing["can_close"] is False
    with session_context_for_test(maker.id):
        implementation = await registry.call(
            "record_attestation",
            {
                "task_id": task.id,
                "kind": "implementation",
                "result": "approved",
                "summary": "The recipe lists its ingredients and baking temperature.",
                "reference": {"scheme": "artifact", "value": str(artifact)},
                "evidence": {"contents": artifact.read_text()},
            },
        )
    assert "error" not in implementation, implementation

    if paired:
        with session_context_for_test(maker.id):
            missing_review = await registry.call(
                "close_task", {"task_id": task.id, "preview": True}
            )
        with session_context_for_test(reviewer.id):
            review = await registry.call(
                "record_attestation",
                {
                    "task_id": task.id,
                    "kind": "review",
                    "result": "approved",
                    "summary": "Independently checked the recipe against both criteria.",
                    "reference": {"scheme": "artifact", "value": str(artifact)},
                },
            )
        assert missing_review["can_close"] is False
        assert "error" not in review, review
        assert tasks.get_task(task.id).claimed_by_session_id == maker.id

    reason = "done"
    if incomplete_attribution:
        variables.set_variable(maker.id, "task_edited_files", {task.id: ["unresolved.txt"]})
        reason = "wont_do"
        with session_context_for_test(maker.id):
            disposition = await registry.call(
                "record_attestation",
                {
                    "task_id": task.id,
                    "kind": "disposition",
                    "result": "approved",
                    "summary": "The operator cancelled this recipe.",
                    "evidence": {"reason": reason},
                },
            )
        assert "error" not in disposition, disposition

    with session_context_for_test(closer.id):
        preview = await registry.call(
            "close_task", {"task_id": task.id, "reason": reason, "preview": True}
        )
        closed = await registry.call("close_task", {"task_id": task.id, "reason": reason})

    if incomplete_attribution:
        assert preview["can_close"] is False, preview
        assert closed["closed"] is False, closed
        assert "attributed changes" in closed["message"]
        assert tasks.get_task(task.id).closed_at is None
        assert tasks.get_task(task.id).claimed_by_session_id == maker.id
        return

    assert preview.get("error") is None, preview.get("message")
    assert preview["can_close"] is True, preview.get("blocking_reasons")
    assert closed["closed"] is True, closed
    stored = tasks.get_task(task.id)
    assert stored.closed_in_session_id == closer.id
    assert stored.closed_commit_sha is None and not stored.commits
    records = list_attestations(temp_db, task.id)
    assert records[0].session_id == maker.id
    assert len(records) == (2 if paired else 1)
    if paired:
        assert records[1].session_id == reviewer.id
    assert not (root / ".git").exists()
    assert artifact.read_text() == "Ingredients: flour, butter, eggs. Bake at 180 C.\n"
