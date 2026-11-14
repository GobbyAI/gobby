"""The generic evidence transport never needs a repository or a supplied author."""

from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions as _Seats
from tests.mcp_proxy.tools.tasks.test_close_commit_links import _commit, _git

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_commit_preview_matches_atomic_policy_and_canonical_link(
    temp_db: HubDatabase, sample_git_project: dict[str, Any]
) -> None:
    project_id = sample_git_project["id"]
    repo = Path(sample_git_project["repo_path"])
    sha = _commit(repo, "Prospective implementation")
    author = _Seats(temp_db, project_id).add(
        "commit-preview-author",
        [],
        definition_fields={
            "workflows": {"rule_selectors": {"include": ["tag:commit-preview"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id, "Commit preview", validation_criteria="Preview proposed links."
    )
    RuleDefinitionManager(temp_db).create(
        "commit-present",
        {
            "event": "task_close",
            "when": "not task.commits",
            "effects": [{"type": "block", "reason": "A commit is required by this policy."}],
        },
        project_id=project_id,
        tags=["commit-preview"],
    )
    registry = create_task_registry(manager)
    arguments = {"task_id": task.id, "commit_sha": sha, "project_path": str(repo)}
    with session_context_for_test(author):
        preview = await registry.call("close_task", {**arguments, "preview": True})
        assert preview["can_close"] is True, preview
        assert manager.get_task(task.id).commits is None
        final = await registry.call(
            "close_task",
            {
                **arguments,
                "expected_closure_revision": preview["snapshot_revision"],
            },
        )
    assert final["closed"] is True, final
    assert final["commit_shas"] == [_git(repo, "rev-parse", "--short", sha)]
    closed = manager.get_task(task.id)
    assert closed.commits == final["commit_shas"]
    assert closed.closed_commit_sha == sha


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["done", "served"])
async def test_non_git_close_preview_and_final_use_selected_attestation_policy(
    temp_db: HubDatabase, sample_project: dict[str, Any], reason: str
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "cake-maker",
        [],
        results=["pass"],
        definition_fields={
            "task_attestation_writes": ["baked"],
            "workflows": {"rule_selectors": {"include": ["tag:cake-close"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        title="Bake cake",
        project_id=sample_project["id"],
        category="manual",
        validation_criteria="Cake is baked.",
    )
    RuleDefinitionManager(temp_db).create(
        "cake-completion",
        {
            "event": "task_close",
            "when": "latest_attestation('baked').get('result') != 'pass'",
            "effects": [{"type": "block", "reason": "Cake evidence missing."}],
        },
        project_id=sample_project["id"],
        tags=["cake-close"],
    )
    registry = create_task_registry(manager)
    with session_context_for_test(author):
        denied = await registry.call(
            "close_task", {"task_id": task.id, "preview": True, "reason": reason}
        )
        assert denied["can_close"] is False
        assert "Cake evidence missing." in denied["blocking_reasons"]
        written = await registry.call(
            "record_attestation",
            {
                "task_id": task.id,
                "kind": "baked",
                "result": "pass",
                "summary": "Baked and cooled.",
            },
        )
        assert written["created"] is True
        preview = await registry.call(
            "close_task", {"task_id": task.id, "preview": True, "reason": reason}
        )
        assert preview["can_close"] is True
        assert preview["policy_revision"]
        assert manager.get_task(task.id).closed_at is None
        final = await registry.call("close_task", {"task_id": task.id, "reason": reason})
    assert final["closed"] is True
    assert final["policy_revision"] == preview["policy_revision"]
    assert manager.get_task(task.id).commits is None
    assert manager.get_task(task.id).closed_reason == reason


@pytest.mark.asyncio
async def test_close_refuses_a_callers_stale_preview_revision(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "preview-owner",
        [],
        results=["pass"],
        definition_fields={"task_attestation_writes": ["checkpoint"]},
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        title="Preview revision",
        project_id=sample_project["id"],
        validation_criteria="Reject changed evidence.",
    )
    registry = create_task_registry(manager)
    with session_context_for_test(author):
        preview = await registry.call("close_task", {"task_id": task.id, "preview": True})
        written = await registry.call(
            "record_attestation",
            {
                "task_id": task.id,
                "kind": "checkpoint",
                "result": "pass",
                "summary": "New evidence.",
            },
        )
        assert written["created"] is True
        refused = await registry.call(
            "close_task",
            {
                "task_id": task.id,
                "expected_closure_revision": preview["snapshot_revision"],
            },
        )
    assert refused["error"] == "close_state_changed"
    assert refused["closed"] is False
    assert manager.get_task(task.id).closed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("brief", [True, False])
async def test_record_and_list_non_git_attestations(
    temp_db: HubDatabase, sample_project: dict[str, Any], brief: bool
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add(
        "book-editor", [], definition_fields={"task_attestation_writes": ["proofread"]}
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        title="Proofread chapter",
        project_id=sample_project["id"],
        validation_criteria="Chapter has been checked.",
    )
    registry = create_task_registry(manager)
    with session_context_for_test(author):
        result = await registry.call(
            "record_attestation",
            {
                "task_id": task.id,
                "kind": "proofread",
                "result": "approved",
                "summary": "Checked all paragraphs.",
                "reference": {"scheme": "manuscript", "value": "book://chapter-1/v3"},
            },
        )
        assert "error" not in result
        assert result["created"] is True
        assert result["record"]["session_id"] == author
        listing = await registry.call("list_attestations", {"task_id": task.id})
        detail = await registry.call("get_task", {"task_id": task.id, "brief": brief})
    assert listing["attestations"] == [result["record"]]
    assert "step_records" not in detail
    assert detail["attestations"][0]["id"] == result["record"]["id"]
    assert detail["attestations"][0]["kind"] == "proofread"
    assert detail["attestations"][0]["result"] == "approved"
    if not brief:
        assert detail["attestations"] == listing["attestations"]


def test_attestation_schema_excludes_client_provenance(temp_db: HubDatabase) -> None:
    registry = create_task_registry(LocalTaskManager(temp_db))
    tool = registry.get_schema("record_attestation")
    assert tool is not None
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "session_id" not in schema["properties"]
    assert "agent_definition_hash" not in schema["properties"]
    assert set(schema["required"]) == {"task_id", "kind", "result", "summary"}


@pytest.mark.parametrize(
    "name",
    ["record_test_step", "record_implementation_step", "record_review_step", "record_closing_step"],
)
def test_task_transport_retires_fixed_workflow_step_writes(temp_db: HubDatabase, name: str) -> None:
    registry = create_task_registry(LocalTaskManager(temp_db))
    assert registry.get_schema(name) is None
    assert registry.get_schema("record_attestation") is not None


@pytest.mark.parametrize("reason", ["", "  ", "x" * 121])
async def test_invalid_reason_metadata_refuses_preview_and_final_without_mutation(
    temp_db: HubDatabase, sample_project: dict[str, Any], reason: str
) -> None:
    author = _Seats(temp_db, sample_project["id"]).add("metadata-author")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Check reason metadata")
    registry = create_task_registry(manager)
    with session_context_for_test(author):
        for preview in (True, False):
            result = await registry.call(
                "close_task", {"task_id": task.id, "reason": reason, "preview": preview}
            )
            assert result["can_close"] is False
            assert result["error"] == "invalid_close_reason"
    assert manager.get_task(task.id).closed_at is None
