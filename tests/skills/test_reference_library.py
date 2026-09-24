"""The reference library must cover public operations and verified guide sections."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.build.service import DispatcherTickSummary
from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.services.result_offload import _WRAPPER_MUTATION_RESERVE
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.mcp_proxy.tools.results import create_results_registry
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import launch_close_review
from gobby.mcp_proxy.tools.workflows._pipeline_execution import resume_pipeline
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.sessions.handoff import (
    HANDOFF_PULL_PENDING_VARIABLE,
    consume_pending_handoff,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.storage.config_mutations import ConfigConflictError, ConfigMutationResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tool_results import ToolResultStore
from gobby.storage.worktrees import Worktree
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks
from tests.mcp_proxy import test_results_tools as result_scenarios
from tests.mcp_proxy.tools import test_config_values as config_scenarios
from tests.mcp_proxy.tools import test_mcp_proxy_tools_build as build_scenarios
from tests.mcp_proxy.tools import test_mcp_proxy_tools_pipeline_resume as pipeline_scenarios
from tests.mcp_proxy.tools import test_worktrees_lifecycle as workspace_scenarios
from tests.mcp_proxy.tools.tasks import test_lifecycle_close_orchestration as task_scenarios
from tests.sessions import test_handoff as handoff_scenarios
from tests.skills.reference_library_helpers import (
    ROOT,
    cli_inventory,
    coverage_errors,
    documentation_errors,
    internal_tool_inventory,
    load_audits,
    local_link_errors,
    markdown_targets,
    native_cli_inventory,
    proxy_tool_inventory,
)

pytestmark = pytest.mark.unit
reference_session_manager = handoff_scenarios.session_manager
_local_machine_identity = handoff_scenarios._local_machine_identity


async def test_reference_contract_3_2_1() -> None:
    audits = load_audits()
    errors = coverage_errors(
        audits,
        internal_tool_inventory() | await proxy_tool_inventory(),
        cli_inventory() | native_cli_inventory(),
    )
    errors.extend(documentation_errors(audits))
    assert errors == [], "\n".join(errors)


@pytest.mark.parametrize(
    "scenario",
    [
        "tasks",
        "planning-build",
        "agent-handoff",
        "pipeline",
        "workspace",
        "config-conflict",
        "oversized-result",
    ],
)
async def test_reference_contract_3_2_2(
    scenario: str,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    reference_session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise and assert representative operations within isolated fixture lifetimes."""
    errors = documentation_errors(load_audits())
    assert errors == [], "\n".join(errors)
    if scenario == "tasks":
        reviewer_run_id = "a1b2c3d4-1234-4567-89ab-123456789abc"
        task_store = task_scenarios._Store(task_scenarios._review(status="queued", run_id=None))
        task_registry = SimpleNamespace(
            call=AsyncMock(return_value={"success": True, "run_id": reviewer_run_id})
        )
        task_ctx = task_scenarios._ctx(
            registry=task_registry,
            validation_config=TaskValidationConfig(
                candidates=["codex/gpt-5.6-terra"],
                close_review_total_timeout_seconds=17,
                close_review_validator_timeout_seconds=900,
            ),
        )
        task_scenarios._patch_store(monkeypatch, task_store)
        task_evaluation = task_scenarios._evaluation()
        task_evaluation.extra["coordinator_owned_pending"] = True
        task_arguments = task_scenarios._arguments()
        task_result = await launch_close_review(
            task_ctx,
            evaluation=task_evaluation,
            close_arguments=task_arguments,
            evaluate_close=task_scenarios._revalidate(task_evaluation),
        )
        assert task_store.created_arguments == {
            **task_arguments,
            "_review_timeout_seconds": 900,
            "_criterion_count": 3,
            "_manifest_count": None,
            "_excerpt_chars": None,
            "_review_provider": "codex",
            "_review_model": "gpt-5.6-terra",
        }
        assert "_review_deadline_at" not in task_arguments
        assert task_evaluation.task is not None
        assert task_store.expected_task_updated_at == task_evaluation.task.updated_at
        task_registry.call.assert_awaited_once()
        task_launch_args = task_registry.call.call_args.args[1]
        assert task_launch_args["agent"] == "task-close-reviewer"
        assert task_launch_args["task_id"] is None
        assert task_launch_args["isolation"] == "none"
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        create_spawn_agent_registry(MagicMock())._prepare_call("spawn_agent", task_launch_args)
        assert task_launch_args["project_path"] == task_evaluation.repo_path
        assert task_launch_args["provider"] == "codex"
        assert task_launch_args["model"] == "gpt-5.6-terra"
        assert task_launch_args["reasoning_effort"] == "auto"
        assert task_launch_args["timeout"] == 900
        assert "close caller is a spawned agent" in task_launch_args["prompt"]
        assert "state `pending_external`" in task_launch_args["prompt"]
        assert task_result["success"] is True
        assert task_result["closed"] is False
        assert task_result["can_close"] is False
        assert task_result["error"] == "close_review_required"
        assert task_result["reviewer_run_id"] == reviewer_run_id
        assert task_result["review_status"] == "running"
        assert task_result["criterion_count"] == 3
        assert task_result["criterion_indexes"] == [1, 2, 3]
        assert "run_id" not in task_result
        assert "spawn_request" not in task_result
        assert "review_run_id" not in task_result
        assert "Do not poll agent runs or re-call close_task." in task_result["message"]
        assert "Oversized" not in task_result["message"]
        assert task_result["close_review_duration_ms"] == 4.25
    elif scenario == "planning-build":
        build_registry = build_scenarios._registry(temp_db)
        build_task = build_registry.get_tool("build_task")
        expected_build = build_scenarios._small_build_result(
            initial_lifecycle="in_development",
            applied_stages_skipped=["qa"],
            tick_dispatched=1,
            dispatcher_tick=DispatcherTickSummary(ticks=1, scanned=3, executed=1, skipped=0),
            dry_run=True,
        )
        with patch(
            "gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=expected_build)
        ) as build_service:
            build_result = await build_task(
                input_ref="#42",
                profile="submit",
                quick=True,
                skip_stages=["qa"],
                workspace_backend="clone",
                unattended=True,
                no_merge=False,
                pr="123",
                stage=["pr:max_review_rounds=2"],
                target_branch="release/0.4",
                agent="backend-developer",
                clones_dir="/tmp",
                cwd="/tmp",
                reset_expansion_output=True,
                max_active_agents=4,
                max_retries=0,
                planning_seed_state="approved",
                completed_plan_review_rounds=2,
                dry_run=True,
                coordinator="#6075",
                project_id="project-1",
            )
        assert build_result == {
            "task_id": "task-1",
            "created": False,
            "initial_lifecycle": "in_development",
            "applied_stages_skipped": ["qa"],
            "tick_dispatched": 1,
            "dispatcher_tick": {
                "ticks": 1,
                "scanned": 3,
                "executed": 1,
                "skipped": 0,
                "cap_reached": False,
                "reason": None,
            },
            "manifest": None,
            "dry_run": True,
        }
        build_call = build_service.call_args
        assert build_call.args[0] == "#42"
        build_opts = build_call.args[1]
        assert build_opts.profile == "submit"
        assert build_opts.quick is True
        assert build_opts.skip_stages == ["qa"]
        assert build_opts.isolation == "clone"
        assert build_opts.isolation_explicit is True
        assert build_opts.unattended is True
        assert build_opts.unattended_explicit is True
        assert str(build_opts.clones_dir) == "/tmp"
        assert str(build_opts.cwd) == "/tmp"
        assert build_opts.no_merge is False
        assert build_opts.pr == "123"
        assert [
            (build_item.stage_name, build_item.max_work_attempts, build_item.max_review_rounds)
            for build_item in build_opts.stage_caps
        ] == [("pr", None, 2)]
        assert build_opts.target_branch == "release/0.4"
        assert build_opts.assigned_agent == "backend-developer"
        assert build_opts.reset_expansion_output is True
        assert build_opts.max_active_agents == 4
        assert build_opts.max_retries == 0
        assert build_opts.planning_seed_state == "approved"
        assert build_opts.completed_plan_review_rounds == 2
        assert build_opts.dry_run is True
        assert build_opts.coordinator_session_ref == "#6075"
        assert build_call.kwargs["db"] is temp_db
        assert build_call.kwargs["project_id"] == "project-1"
        assert "services" in build_call.kwargs
    elif scenario == "agent-handoff":
        handoff_predecessor = handoff_scenarios._registered_session(reference_session_manager)
        handoff_payload = build_handoff_payload(current_state="Ready.", next_steps=["Continue."])
        handoff_compact_state = stage_handoff_attempt(
            temp_db,
            handoff_predecessor.id,
            attempt_id="a" * 32,
            handoff=handoff_payload,
            clear_session=False,
        )
        handoff_sv_mgr = SessionVariableManager(temp_db)
        assert (
            handoff_sv_mgr.get_variables(handoff_predecessor.id).get(HANDOFF_PULL_PENDING_VARIABLE)
            is True
        )
        handoff_compact = consume_pending_handoff(temp_db, handoff_predecessor.id)
        assert (
            handoff_compact is not None
            and handoff_compact.markdown == handoff_payload.rendered_markdown
        )
        handoff_compact_receipt = temp_db.fetchone(
            "SELECT * FROM session_handoff_deliveries WHERE handoff_id = %s",
            (handoff_compact_state.handoff_record_id,),
        )
        assert handoff_compact_receipt is not None
        assert handoff_compact_receipt["boundary_kind"] == "compact"
        assert HANDOFF_PULL_PENDING_VARIABLE not in handoff_sv_mgr.get_variables(
            handoff_predecessor.id
        )
        assert consume_pending_handoff(temp_db, handoff_predecessor.id) is None
        handoff_clear_state = stage_handoff_attempt(
            temp_db,
            handoff_predecessor.id,
            attempt_id="b" * 32,
            handoff=handoff_payload,
            clear_session=True,
        )
        assert HANDOFF_PULL_PENDING_VARIABLE not in handoff_sv_mgr.get_variables(
            handoff_predecessor.id
        )
        handoff_successor_id = reference_session_manager.register_session(
            external_id="clear-successor",
            machine_id=handoff_scenarios.MACHINE_ID,
            source="codex",
            project_id=handoff_predecessor.project_id,
            parent_session_id=handoff_predecessor.id,
        )
        record_handoff_delivery(
            temp_db,
            handoff_id=handoff_clear_state.handoff_record_id,
            attempt_id="b" * 32,
            boundary_kind="clear",
            continuation_session_id=handoff_successor_id,
        )
        handoff_sv_mgr.merge_variables(handoff_successor_id, {HANDOFF_PULL_PENDING_VARIABLE: True})
        handoff_cleared = consume_pending_handoff(temp_db, handoff_successor_id)
        assert handoff_cleared is not None and handoff_cleared.session_id == handoff_predecessor.id
        assert HANDOFF_PULL_PENDING_VARIABLE not in handoff_sv_mgr.get_variables(
            handoff_successor_id
        )
        assert consume_pending_handoff(temp_db, handoff_successor_id) is None
    elif scenario == "pipeline":
        pipeline_execution = pipeline_scenarios._make_execution(
            status=ExecutionStatus.FAILED, inputs_json=json.dumps({"branch": "main"})
        )
        pipeline_definition = pipeline_scenarios._make_pipeline()
        pipeline_failed_step = MagicMock()
        pipeline_failed_step.step_id = "failed-step"
        pipeline_failed_step.status = StepStatus.FAILED
        pipeline_failed_step.error = "boom"
        pipeline_arrivals = 0
        pipeline_both_loading = asyncio.Event()

        async def load_pipeline(pipeline_name: str, pipeline_project_id: str) -> MagicMock:
            nonlocal pipeline_arrivals
            assert pipeline_name == pipeline_execution.pipeline_name
            assert pipeline_project_id == pipeline_execution.project_id
            pipeline_arrivals += 1
            if pipeline_arrivals == 2:
                pipeline_both_loading.set()
            await pipeline_both_loading.wait()
            return pipeline_definition

        pipeline_loader = MagicMock()
        pipeline_loader.load_pipeline = AsyncMock(side_effect=load_pipeline)
        pipeline_executor = MagicMock()
        pipeline_executor.execute = AsyncMock(return_value=pipeline_execution)
        pipeline_execution_manager = MagicMock()
        pipeline_execution_manager.get_execution.return_value = pipeline_execution
        pipeline_execution_manager.get_steps_for_execution.return_value = [pipeline_failed_step]
        pipeline_execution_manager.reset_steps_from.return_value = 1
        pipeline_execution_manager.claim_failed_execution_for_resume.side_effect = [
            pipeline_execution,
            None,
        ]
        pipeline_results = await asyncio.gather(
            resume_pipeline(
                loader=pipeline_loader,
                executor=pipeline_executor,
                execution_manager=pipeline_execution_manager,
                execution_id=pipeline_execution.id,
                project_id=pipeline_execution.project_id,
            ),
            resume_pipeline(
                loader=pipeline_loader,
                executor=pipeline_executor,
                execution_manager=pipeline_execution_manager,
                execution_id=pipeline_execution.id,
                project_id=pipeline_execution.project_id,
            ),
        )
        await drain_asyncio_tasks(cycles=2)
        pipeline_winners = [
            pipeline_result for pipeline_result in pipeline_results if pipeline_result["success"]
        ]
        pipeline_losers = [
            pipeline_result
            for pipeline_result in pipeline_results
            if not pipeline_result["success"]
        ]
        assert len(pipeline_winners) == 1
        assert pipeline_winners[0]["status"] == "resuming"
        assert len(pipeline_losers) == 1
        assert "already being resumed" in pipeline_losers[0]["error"]
        assert pipeline_execution_manager.claim_failed_execution_for_resume.call_count == 2
        pipeline_execution_manager.reset_steps_from.assert_called_once_with(
            pipeline_execution.id, "failed-step"
        )
        pipeline_executor.execute.assert_awaited_once()
    elif scenario == "workspace":
        worktree_storage = MagicMock()
        worktree_storage.resolve_reference.side_effect = lambda workspace_ref: workspace_ref
        worktree = Worktree(
            id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            project_id="p1",
            branch_name="b1",
            worktree_path="/tmp/p1",
            base_branch="main",
            status="active",
            created_at=workspace_scenarios._VALID_TIMESTAMP,
            updated_at=workspace_scenarios._VALID_TIMESTAMP,
            task_id=None,
            agent_session_id=None,
            merged_at=None,
        )
        worktree_storage.get.return_value = worktree
        workspace_registry = create_worktrees_registry(
            worktree_storage=worktree_storage,
            git_manager=None,
            project_id="11111111-1111-4111-8111-111111110001",
        )
        with patch("pathlib.Path.exists", return_value=True):
            workspace_result = await workspace_registry.call(
                "delete_worktree", {"worktree_id": worktree.id}
            )
        assert workspace_result["success"] is False
        assert "without a resolved git manager" in workspace_result["error"]
        worktree_storage.delete.assert_not_called()
    elif scenario == "config-conflict":
        config_service, _runtime, config_mutations = config_scenarios._service(
            config_scenarios._snapshot(3), result=ConfigMutationResult(4, frozenset())
        )
        config_registry = create_config_registry(lambda: config_service)
        config_schema = config_registry.get_schema("patch_config_values")
        assert config_schema is not None
        assert config_schema["inputSchema"]["required"] == ["expected_revision"]
        with pytest.raises(TypeError):
            await config_registry.call("patch_config_values", {"values": {}})
        config_managed = await config_registry.call(
            "patch_config_values",
            {"expected_revision": 3, "values": {"ai": {"embeddings": {"model": "replacement"}}}},
        )
        config_secret = await config_registry.call(
            "patch_config_values",
            {
                "expected_revision": 3,
                "values": {
                    "ai": {
                        "generation": {
                            "endpoints": {"openrouter": {"api_key": "classified-secret"}}
                        }
                    }
                },
            },
        )
        assert config_managed["error"]["code"] == "managed_activation_required"
        assert config_secret == {
            "committed": True,
            "revision": 4,
            "changed_keys": [],
            "apply_status": "applied",
            "pending_restart_keys": [],
            "failed_live_keys": {},
        }
        config_secret_patch = config_mutations.calls[-1][1]
        assert config_secret_patch.values == {}
        assert (
            config_secret_patch.secrets["ai.generation.endpoints.openrouter.api_key"].plaintext
            == "classified-secret"
        )
        config_conflict_service, _runtime, _mutations = config_scenarios._service(
            config_scenarios._snapshot(4), error=ConfigConflictError(3, 4)
        )
        config_conflict = await create_config_registry(lambda: config_conflict_service).call(
            "patch_config_values", {"expected_revision": 3, "values": {}}
        )
        assert config_conflict == {
            "error": {
                "code": "revision_conflict",
                "message": "Configuration revision is stale",
                "path": ["expected_revision"],
                "retryable": True,
                "expected_revision": 3,
                "actual_revision": 4,
            }
        }
    elif scenario == "oversized-result":
        result_config = result_scenarios._config()
        result_store = ToolResultStore(temp_db, result_config)
        result_content = "".join(str(result_index % 10) for result_index in range(5000))
        result_id = result_scenarios._save(
            result_store, project_id=sample_project["id"], content=result_content, total_chars=5500
        )
        result_registry = create_results_registry(
            temp_db, result_config, default_project_id=sample_project["id"]
        )
        result_page = await result_registry.call(
            "get_tool_result",
            {
                "result_id": result_id,
                "offset": 100,
                "limit": result_config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE,
            },
        )
        assert result_page["content"] == result_content[100 : 100 + len(result_page["content"])]
        assert result_page["offset"] == 100
        assert result_page["next_offset"] == 100 + len(result_page["content"])
        assert result_page["total_chars"] == 5500
        assert result_page["stored_chars"] == 5000
        assert (
            result_scenarios._serialized_size(result_page)
            <= result_config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
        )


def test_unmapped_public_operations_are_rejected() -> None:
    errors = coverage_errors(
        load_audits(), {("gobby-future", "new_operation")}, {"gobby future operation"}
    )
    assert errors == [
        "Unmapped public tool: gobby-future:new_operation",
        "Unmapped public CLI: gobby future operation",
    ]


def test_unaudited_guide_link_is_rejected() -> None:
    audits = deepcopy(load_audits())
    for audit in audits:
        audit["guides"] = [
            guide for guide in audit["guides"] if guide["path"] != "docs/guides/tasks.md"
        ]
    errors = documentation_errors(audits)
    assert any(
        error.startswith("Unaudited guide link:") and "docs/guides/tasks.md" in error
        for error in errors
    ), errors


def test_markdown_ignores_code_comments_and_resolves_reference_links(tmp_path: Path) -> None:
    document = tmp_path / "reference.md"
    document.write_text(
        "# Actual `heading`\n\n```sh\n# Fake heading\n```\n\n"
        "# Actual `heading`\n\n[Guide][guide]\n\n[guide]: guide.md#section\n"
    )
    assert markdown_targets(document) == (
        {"actual-heading", "actual-heading-1"},
        ["guide.md#section"],
    )


def test_all_absorbed_skills_have_one_audit_owner() -> None:
    expected = {
        "tasks",
        "live-session",
        "plan",
        "plan-draft",
        "plan-enhance",
        "plan-mechanic",
        "plan-review",
        "expand",
        "expansion-agent-selection",
        "build",
        "build-coordinator",
        "handoff-discipline",
        "persona",
        "memory",
        "review-learning",
        "code-index",
        "loading-skills",
        "writing-skills",
        "build-rule",
        "mcp-servers",
        "pipelines-and-cron",
        "source-control",
        "clones",
        "merge",
        "merge-expert",
        "review",
        "epic-review",
        "intro",
        "development-discipline",
        "channel-parity",
    }
    absorbed = [name for audit in load_audits() for name in audit["source_skills"]]
    assert set(absorbed) == expected
    assert len(absorbed) == len(expected)


def test_missing_operation_evidence_is_rejected() -> None:
    audits = deepcopy(load_audits())
    audits[0]["operations"][0]["verification"] = ["unrecorded-proof"]
    assert any(
        error.startswith("Missing operation evidence:") for error in documentation_errors(audits)
    )


@pytest.mark.parametrize("incomplete", ["audit", "guide", "evidence"])
def test_incomplete_verification_is_rejected(incomplete: str) -> None:
    audits = deepcopy(load_audits())
    if incomplete == "audit":
        audits[0]["status"] = "in_progress"
    elif incomplete == "guide":
        audits[0]["guides"][0]["audit_status"] = "in_progress"
    else:
        audits[0]["evidence"][0]["result"] = ""
        audits[0]["evidence"][0]["description"] = ""
    assert any(
        "not verified" in error or "incomplete evidence" in error
        for error in documentation_errors(audits)
    )


def test_same_document_anchor_is_checked(tmp_path: Path) -> None:
    document = tmp_path / "reference.md"
    document.write_text("# Heading\n\n[Valid](#heading)\n")
    assert local_link_errors(document) == []
    document.write_text("# Heading\n\n[Broken](#missing)\n")
    assert local_link_errors(document) == [f"Broken anchor: {document} -> #missing"]


@pytest.mark.parametrize("invalid_field", ["reference", "verification"])
def test_native_mapping_requires_reference_and_evidence(invalid_field: str) -> None:
    audits = deepcopy(load_audits())
    native = next(audit["native_cli"][0] for audit in audits if audit.get("native_cli"))
    native[invalid_field] = "missing.md" if invalid_field == "reference" else ["missing-proof"]
    expected = (
        "Uncataloged operation reference:"
        if invalid_field == "reference"
        else "Missing operation evidence:"
    )
    assert any(error.startswith(expected) for error in documentation_errors(audits))


def test_new_native_command_cannot_hide_behind_stale_contract(tmp_path: Path) -> None:
    for relative in (
        "tests/contracts/gcode.contract.json",
        "crates/gcode/src/contract.rs",
        "crates/gcode/src/cli.rs",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
    source = tmp_path / "crates/gcode/src/cli.rs"
    source.write_text(
        source.read_text().replace("enum Command {", "enum Command {\n    FutureOperation,")
    )
    with pytest.raises(ValueError, match="Clap/contract command drift.*future-operation"):
        native_cli_inventory(tmp_path)


SCENARIO_OPERATION_REFERENCES: dict[str, tuple[str, ...]] = {
    "create_task": ("gobby:references/tasks/creation.md",),
    "edit": ("gobby:references/tasks/implementation.md",),
    "preview_close": ("gobby:references/tasks/closing.md",),
    "review_memory": ("gobby:references/memory/post-task.md",),
    "fix_finding": ("gobby:references/tasks/implementation.md",),
    "close_current_task": ("gobby:references/tasks/closing.md",),
    "set_handoff_compact": ("gobby:references/sessions/handoffs.md",),
    "set_handoff_clear": ("gobby:references/sessions/handoffs.md",),
    "get_agent_result": ("gobby:references/agents/lifecycle.md",),
    "get_agent_capture_pages": (
        "gobby:references/agents/lifecycle.md",
        "gobby:references/mcp-servers/results.md",
    ),
    "draft_plan": ("gobby:references/plan/drafting.md", "gobby:references/plan/coverage.md"),
    "stage_handoff": ("gobby:references/sessions/handoffs.md",),
    "set_handoff": ("gobby:references/sessions/handoffs.md",),
    "save_plan": ("gobby:references/plan/drafting.md", "gobby:references/plan/coverage.md"),
    "hand_to_task_workflow": (
        "gobby:references/tasks/overview.md",
        "gobby:references/tasks/creation.md",
    ),
    "derive_plan_handoff_manifest": ("gobby:references/plan/approval.md",),
    "apply_plan_handoff_manifest": ("gobby:references/plan/approval.md",),
    "write_pipeline_yaml": ("gobby:references/pipelines/authoring.md",),
    "validate_pipeline_definition": ("gobby:references/pipelines/validation.md",),
    "create_pipeline": ("gobby:references/pipelines/authoring.md",),
    "run_pipeline": ("gobby:references/pipelines/execution.md",),
    "create_cron_job": ("gobby:references/pipelines/scheduling.md",),
    "run_cron_job": ("gobby:references/pipelines/scheduling.md",),
    "gcode_search": ("gobby:references/code-index/search.md",),
    "gcode_outline": ("gobby:references/code-index/retrieval.md",),
    "gcode_symbol": ("gobby:references/code-index/retrieval.md",),
    "gcode_sibling_sweep": ("gobby:references/code-index/search.md",),
    "apply_bounded_repair": (
        "gobby:references/plan/repair.md",
        "gobby:references/plan/drafting.md",
        "gobby:references/plan/coverage.md",
    ),
    "capture_findings_in_owning_section": (
        "gobby:references/plan/drafting.md",
        "gobby:references/plan/coverage.md",
    ),
    "consult_event_table": ("gobby:references/rules/events.md",),
    "author_rules": (
        "gobby:references/rules/authoring.md",
        "gobby:references/rules/events.md",
        "gobby:references/rules/effects.md",
    ),
    "create_coordination_epic": ("gobby:references/tasks/creation.md",),
    "normalize_leaf_stages": (
        "gobby:references/tasks/reviews.md",
        "gobby:references/build/stages.md",
    ),
    "launch_build": ("gobby:references/build/starting.md",),
    "monitor_dispatch": ("gobby:references/build/monitoring.md",),
    "fix_actionable_coordination_bug": ("gobby:references/tasks/implementation.md",),
    "monitor_agents": ("gobby:references/agents/lifecycle.md",),
    "set_handoff_before_agent_wait": ("gobby:references/sessions/handoffs.md",),
    "wait_for_agent_once_to_subscribe": (
        "gobby:references/agents/lifecycle.md",
        "gobby:references/sessions/waits.md",
    ),
    "close_target": ("gobby:references/tasks/closing.md",),
    "close_coordination_epic": ("gobby:references/tasks/closing.md",),
}


def test_reference_contract_5_2_3() -> None:
    """Replay recorded obligations and reject scenarios loading retired instructions."""
    from gobby.skills.instruction_requirements import parse_instruction_requirement
    from tests.skills.scenario_runner import run_recorded_skill_scenario

    root = Path(__file__).resolve().parents[2]
    bundled = root / "src/gobby/install/shared/skills"
    scenarios = sorted((root / "tests/skills/scenarios").rglob("*.yaml"))
    migrated = 0
    for path in scenarios:
        result = run_recorded_skill_scenario(path)
        requirement = parse_instruction_requirement(result.skill)
        instruction = bundled / requirement.skill / (requirement.path or "SKILL.md")
        assert instruction.is_file(), (path, result.skill)
        if requirement.path is not None:
            migrated += 1
        for identity in result.loaded.loaded_skills:
            loaded = parse_instruction_requirement(identity)
            assert (bundled / loaded.skill / (loaded.path or "SKILL.md")).is_file(), (
                path,
                identity,
            )
        available = set(result.loaded.loaded_skills)
        for action in result.loaded.actions:
            if requirement.skill == "gobby":
                required = SCENARIO_OPERATION_REFERENCES.get(action["action"], ())
                assert set(required) <= available, (path, action["action"], required, available)
            if action.get("action") == "load_reference":
                available.add(f"{requirement.skill}:{action['path']}")
                assert (bundled / requirement.skill / action["path"]).is_file(), (
                    path,
                    action["path"],
                )
    assert migrated == 21
