from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_requirements import (
    ANCHOR_TARGET_FIELD,
    PLAN_ACCEPT_CAPTURED_BY,
    REQUEST_ANCHOR_VARIABLE,
    append_request_anchor,
    assemble_requirements_bundle,
    build_request_anchor,
    capture_plan_accept_anchor,
    capture_request_anchor,
    is_request_acknowledgement,
    parse_requirement_source_paths,
    plan_accept_anchor_matches,
    validate_request_anchor,
    validate_source_citation,
)
from gobby.plans.review_terminal import _taskless_request_anchor_changed
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


def _plan(*markers: str) -> bytes:
    return (
        "\n".join(
            [
                "# Requirements Review",
                "**Plan ID:** requirements-review",
                "",
                "## Overview",
                "`kind: framing`",
                "",
                "Ordinary reference: docs/reference-only.md",
                "",
                "## Constraints",
                "`kind: framing`",
                "",
                *markers,
                "",
                "```text",
                "requirement-source: docs/fenced.md",
                "```",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Work",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds yet.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "- title: Implement requirements review",
                "  source_section: '1.1'",
                "  covers:",
                "    - 1.1.1",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels:",
                "    - covers:requirements-review:1.1:1.1.1",
                "  description: Implement requirements review behavior.",
                "  validation_criteria: Requirements review behavior is tested.",
                "```",
                "",
            ]
        )
    ).encode()


def _write_requirement(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_requirement_marker_grammar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _write_requirement(tmp_path, "docs/canonical.md", "Canonical requirement.\n")
    snapshot = _plan(
        "requirement-source: docs/canonical.md",
        "requirement-source: docs/canonical.md",
        "See docs/reference-only.md for background.",
    )

    assert parse_requirement_source_paths(snapshot) == ("docs/canonical.md",)
    assert parse_requirement_source_paths(_plan("  - requirement-source: docs/canonical.md")) == (
        "docs/canonical.md",
    )
    bundle = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=snapshot,
        task_id="task-1",
        task_fields={
            "title": "Title",
            "description": "Description",
            "validation_criteria": "Acceptance",
        },
    )
    sources = cast(list[dict[str, object]], bundle["sources"])
    assert [source.get("path") for source in sources if "path" in source] == ["docs/canonical.md"]

    with pytest.raises(ReviewEvidenceError, match="malformed requirement-source"):
        parse_requirement_source_paths(_plan("requirement-source:"))

    outside_constraints = _plan().replace(
        b"Ordinary reference: docs/reference-only.md",
        b"requirement-source: docs/canonical.md",
    )
    with pytest.raises(ReviewEvidenceError, match="must appear in ## Constraints"):
        parse_requirement_source_paths(outside_constraints)

    with pytest.raises(ReviewEvidenceError, match="repository-relative"):
        assemble_requirements_bundle(
            project_root=tmp_path,
            plan_snapshot=_plan("requirement-source: ../outside.md"),
            request_anchor=build_request_anchor("request-1", "Build the plan"),
        )

    with pytest.raises(ReviewEvidenceError, match="missing"):
        assemble_requirements_bundle(
            project_root=tmp_path,
            plan_snapshot=_plan("requirement-source: docs/missing.md"),
            request_anchor=build_request_anchor("request-1", "Build the plan"),
        )

    original_read_bytes = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == canonical:
            raise PermissionError("denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ReviewEvidenceError, match="unreadable"):
        assemble_requirements_bundle(
            project_root=tmp_path,
            plan_snapshot=snapshot,
            request_anchor=build_request_anchor("request-1", "Build the plan"),
        )


def test_stage_native_no_live_task_access(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_requirement(tmp_path, "docs/canonical.md", "Repository requirement.\n")
    project = LocalProjectManager(temp_db).create(
        name="stage-native-requirements",
        repo_path=str(tmp_path),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="Parent title",
        task_type="review_anchor",
        description="Parent description",
        category="planning",
        validation_criteria="Parent acceptance",
    )
    plan_path = tmp_path / "plan.md"
    plan_path.write_bytes(_plan("requirement-source: docs/canonical.md"))
    service = PlanReviewEvidenceService(temp_db)
    prepared = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="planning",
    )

    def fail_live_task_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("review transport must not read the live task")

    monkeypatch.setattr(service.tasks, "get_task", fail_live_task_read)
    payload = service.snapshot_payload(prepared.evidence_id)
    context = payload["prior_round_context"]
    assert isinstance(context, dict)
    bundle = context["requirements_bundle"]
    assert isinstance(bundle, dict)
    sources = cast(list[dict[str, object]], bundle["sources"])
    assert [source["source_kind"] for source in sources] == [
        "task_field",
        "task_field",
        "task_field",
        "repository_document",
    ]
    assert all(source["content_sha256"] for source in sources)


def test_reviewer_contracts_consume_bundle_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    contracts = [
        root / "src/gobby/install/shared/skills/plan-review/SKILL.md",
        root / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml",
        root / "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml",
    ]

    for contract in contracts:
        content = contract.read_text(encoding="utf-8")
        assert "requirements bundle" in content
        assert "requirement_id" in content
        assert "content_sha256" in content
    staged = contracts[1].read_text(encoding="utf-8")
    taskless = contracts[2].read_text(encoding="utf-8")
    assert "do not fetch the live parent task" in staged
    assert "never substitute the live initiating request" in " ".join(taskless.split())


def test_bundle_representation_properties(tmp_path: Path) -> None:
    canonical = _write_requirement(tmp_path, "docs/canonical.md", "Version one.\n")
    snapshot = _plan(
        "requirement-source: docs/canonical.md",
        "requirement-source: docs/canonical.md",
        "Supporting document: docs/reference-only.md",
    )
    task_fields = {
        "title": "Parent title",
        "description": "Parent description",
        "validation_criteria": "Parent acceptance",
    }
    first = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=snapshot,
        task_id="task-1",
        task_fields=task_fields,
    )
    second = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=snapshot,
        task_id="task-1",
        task_fields=task_fields,
    )
    request_anchor = build_request_anchor(
        "request-1",
        ["First request line\nSecond request line", "Follow-up requirement"],
    )
    taskless = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=snapshot,
        request_anchor=request_anchor,
    )

    first_sources = cast(list[dict[str, object]], first["sources"])
    second_sources = cast(list[dict[str, object]], second["sources"])
    taskless_sources = cast(list[dict[str, object]], taskless["sources"])
    assert first_sources == second_sources
    assert len(first_sources) == 4
    assert len(taskless_sources) == 2
    assert all(
        len(cast(str, source["requirement_id"])) <= 20
        and len(cast(str, source["content_sha256"])) == 64
        and cast(str, source["content_sha256"]).islower()
        for source in first_sources + taskless_sources
    )
    assert "docs/reference-only.md" not in {
        source.get("path") for source in first_sources + taskless_sources
    }
    request_source = next(
        source for source in taskless_sources if source["source_kind"] == "request_anchor"
    )
    assert request_source["content"] == (
        "--- request message 1 ---\n"
        "First request line\n"
        "Second request line\n"
        "--- request message 2 ---\n"
        "Follow-up requirement"
    )
    assert request_source["anchor_content_sha256"] == request_anchor["content_sha256"]
    assert request_source["content_sha256"] != request_anchor["content_sha256"]
    assert (
        validate_source_citation(
            {
                "requirement_id": request_source["requirement_id"],
                "content_sha256": request_source["content_sha256"],
                "line_start": 2,
                "line_end": 4,
            },
            requirements_bundle=taskless,
        )["line_end"]
        == 4
    )

    document_source = next(source for source in first_sources if "path" in source)
    original_id = document_source["requirement_id"]
    original_hash = document_source["content_sha256"]
    canonical.write_text("Version two.\n", encoding="utf-8")
    changed = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=snapshot,
        task_id="task-1",
        task_fields=task_fields,
    )
    changed_source = next(
        source for source in cast(list[dict[str, object]], changed["sources"]) if "path" in source
    )
    assert changed_source["requirement_id"] == original_id
    assert changed_source["content_sha256"] != original_hash


def test_anchor_reuse_and_missing_anchor_fails_closed(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="requirements", repo_path=str(tmp_path))
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="requirements-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / "plan.md"
    plan_path.write_bytes(_plan())
    variables = SessionVariableManager(temp_db)
    anchor = build_request_anchor("request-1", "Original initiating request")
    variables.merge_variables(session.id, {REQUEST_ANCHOR_VARIABLE: anchor})
    service = PlanReviewEvidenceService(temp_db)

    first = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=session.id,
    )
    first_row = service.get_evidence(first.evidence_id)
    first_bundle = cast(dict[str, object], first_row.prior_round_context)["requirements_bundle"]
    service.expire_plan_review_evidence(first.evidence_id, spawn_failed=True)
    variables.merge_variables(session.id, {"skill_authored_request": "replacement"})
    service = PlanReviewEvidenceService(temp_db)

    second = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=2,
        session_id=session.id,
    )
    second_row = service.get_evidence(second.evidence_id)
    second_bundle = cast(dict[str, object], second_row.prior_round_context)["requirements_bundle"]
    assert first_bundle == second_bundle
    service.expire_plan_review_evidence(second.evidence_id, spawn_failed=True)

    missing_session = sessions.register(
        external_id="requirements-missing",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    with pytest.raises(ReviewEvidenceError) as missing:
        service.prepare_plan_review_round(
            project_id=project.id,
            plan_path=plan_path,
            round_number=1,
            session_id=missing_session.id,
        )
    assert missing.value.code == "missing_request_anchor"


def test_request_anchor_hashes_exact_content() -> None:
    content = "Exact request bytes: 🧪\n"
    anchor = build_request_anchor("request-1", content)
    assert anchor["content"] == [content]
    encoded = json.dumps([content], separators=(",", ":"), ensure_ascii=False).encode()
    assert anchor["content_sha256"] == hashlib.sha256(encoded).hexdigest()

    forged = {**anchor, "captured_by": "plan_coordinator"}
    with pytest.raises(ReviewEvidenceError, match="not owned by a recognized capture surface"):
        assemble_requirements_bundle(
            project_root=Path.cwd(),
            plan_snapshot=b"# Plan\n",
            request_anchor=forged,
        )


def test_plan_accept_anchor_builds_validates_and_matches(tmp_path: Path) -> None:
    command = "$gobby plan-accept .gobby/plans/styling.md run two unattended adversarial rounds"
    variables: dict[str, object] = {}
    anchor = capture_plan_accept_anchor(
        variables,
        anchor_id="request-1",
        content=command,
        target_plan_path=".gobby/plans/styling.md",
    )
    assert anchor is not None
    assert variables[REQUEST_ANCHOR_VARIABLE] is anchor

    validated = validate_request_anchor(anchor)
    assert validated["captured_by"] == PLAN_ACCEPT_CAPTURED_BY
    assert validated["content"] == [command]
    assert validated[ANCHOR_TARGET_FIELD] == ".gobby/plans/styling.md"

    assert plan_accept_anchor_matches(
        anchor, project_root=tmp_path, plan_path=".gobby/plans/styling.md"
    )
    assert plan_accept_anchor_matches(
        anchor,
        project_root=tmp_path,
        plan_path=str(tmp_path / ".gobby/plans/styling.md"),
    )
    assert not plan_accept_anchor_matches(
        anchor, project_root=tmp_path, plan_path=".gobby/plans/other.md"
    )


def test_plan_accept_target_field_is_kind_scoped() -> None:
    variables: dict[str, object] = {}
    accept = capture_plan_accept_anchor(
        variables,
        anchor_id="request-1",
        content="$gobby plan-accept plan.md",
        target_plan_path="plan.md",
    )
    assert accept is not None

    missing_target = {key: value for key, value in accept.items() if key != ANCHOR_TARGET_FIELD}
    with pytest.raises(ReviewEvidenceError, match="requires a non-empty target plan path"):
        validate_request_anchor(missing_target)

    observer = build_request_anchor("request-1", "Original initiating request")
    with pytest.raises(ReviewEvidenceError, match="only valid on a plan-accept anchor"):
        validate_request_anchor({**observer, ANCHOR_TARGET_FIELD: "plan.md"})


def test_append_leaves_plan_accept_anchor_byte_identical() -> None:
    variables: dict[str, object] = {}
    anchor = capture_plan_accept_anchor(
        variables,
        anchor_id="request-1",
        content="$gobby plan-accept plan.md",
        target_plan_path="plan.md",
    )
    result = append_request_anchor(
        variables,
        content="substantive follow-up message",
        anchor_id="request-2",
    )
    assert result is None
    assert variables[REQUEST_ANCHOR_VARIABLE] == anchor


def test_plan_accept_anchor_seals_and_scopes_to_target_plan(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="plan-accept", repo_path=str(tmp_path))
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="plan-accept-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / "plan.md"
    plan_path.write_bytes(_plan())
    command = "$gobby plan-accept plan.md run two unattended adversarial rounds"
    variables = SessionVariableManager(temp_db)
    holder: dict[str, object] = {}
    anchor = capture_plan_accept_anchor(
        holder,
        anchor_id="request-accept",
        content=command,
        target_plan_path="plan.md",
    )
    assert anchor is not None
    variables.merge_variables(session.id, {REQUEST_ANCHOR_VARIABLE: anchor})
    service = PlanReviewEvidenceService(temp_db)

    prepared = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=session.id,
    )
    row = service.get_evidence(prepared.evidence_id)
    bundle = cast(dict[str, object], row.prior_round_context)["requirements_bundle"]
    sources = cast(list[dict[str, object]], cast(dict[str, object], bundle)["sources"])
    assert sources[0]["source_kind"] == "request_anchor"
    assert sources[0]["content"] == f"--- request message 1 ---\n{command}"
    service.expire_plan_review_evidence(prepared.evidence_id, spawn_failed=True)

    mismatched = capture_plan_accept_anchor(
        holder,
        anchor_id="request-accept-2",
        content="$gobby plan-accept other-plan.md",
        target_plan_path="other-plan.md",
    )
    assert mismatched is not None
    variables.merge_variables(session.id, {REQUEST_ANCHOR_VARIABLE: mismatched})
    with pytest.raises(ReviewEvidenceError, match="plan-accept anchor targets") as excinfo:
        service.prepare_plan_review_round(
            project_id=project.id,
            plan_path=plan_path,
            round_number=1,
            session_id=session.id,
        )
    assert excinfo.value.code == "invalid_request_anchor"


def test_anchor_migration_is_lossless() -> None:
    original = "Original v1 request bytes: 🧪\n"
    v1_anchor = {
        "version": 1,
        "anchor_id": "legacy-request",
        "content": original,
        "content_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "captured_by": "plan_mode_observer",
    }
    variables: dict[str, object] = {REQUEST_ANCHOR_VARIABLE: v1_anchor}

    migrated = append_request_anchor(
        variables,
        content="Current substantive request\n",
        anchor_id="ignored-fallback",
    )

    assert migrated is not None
    assert migrated["anchor_id"] == "legacy-request"
    assert migrated["content"] == [original, "Current substantive request\n"]
    assert validate_request_anchor(migrated) == migrated

    malformed = {
        "version": 1,
        "content": "malformed raw content\n",
        "unexpected": ["preserve", {"every": "byte"}],
    }
    variables = {REQUEST_ANCHOR_VARIABLE: malformed}
    recovered = append_request_anchor(
        variables,
        content="Replacement request",
        anchor_id="current-request",
    )

    assert recovered is not None
    assert recovered["content"] == ["Replacement request"]
    assert recovered["migration_evidence"] == malformed
    assert validate_request_anchor(recovered)["migration_evidence"] == malformed


def test_ack_predicate_exact_set() -> None:
    acknowledgements = {
        "ok",
        "okay",
        "k",
        "y",
        "yes",
        "yep",
        "yeah",
        "sure",
        "go",
        "go ahead",
        "proceed",
        "continue",
        "do it",
        "sounds good",
        "lgtm",
        "approved",
        "thanks",
        "thank you",
        "ty",
        "👍",
    }
    for acknowledgement in acknowledgements:
        assert is_request_acknowledgement(f" \t{acknowledgement.swapcase()}?!\n")

    near_misses = (
        "ok but rename the flag",
        "yes, after adding tests",
        "go ahead with option two",
        "approved except for naming",
    )
    assert all(not is_request_acknowledgement(content) for content in near_misses)

    variables: dict[str, object] = {
        REQUEST_ANCHOR_VARIABLE: build_request_anchor("prior-plan", "Prior plan request")
    }
    assert capture_request_anchor(variables, anchor_id="new-plan", content=" OK!!! ") is None
    assert REQUEST_ANCHOR_VARIABLE not in variables

    exact_near_miss = "  ok but rename the flag\n"
    seeded = append_request_anchor(
        variables,
        content=exact_near_miss,
        anchor_id="new-plan",
    )
    assert seeded is not None
    assert seeded["content"] == [exact_near_miss]
    assert append_request_anchor(variables, content="Thanks...", anchor_id="new-plan") == seeded


def test_taskless_retry_requires_changed_anchor_hash(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="anchor-retry", repo_path=str(tmp_path))
    session = SessionManager(temp_db).register(
        external_id="anchor-retry-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    original = build_request_anchor("request-1", "Original request")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session.id, {REQUEST_ANCHOR_VARIABLE: original})
    bundle = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=_plan(),
        request_anchor=original,
    )
    context = {"requirements_bundle": bundle}

    assert not _taskless_request_anchor_changed(
        temp_db,
        session_id=session.id,
        prior_round_context=context,
    )

    changed = build_request_anchor("request-1", ["Original request", "Added requirement"])
    variables.merge_variables(session.id, {REQUEST_ANCHOR_VARIABLE: changed})
    assert _taskless_request_anchor_changed(
        temp_db,
        session_id=session.id,
        prior_round_context=context,
    )

    next_plan = build_request_anchor("request-2", "Different plan")
    variables.merge_variables(session.id, {REQUEST_ANCHOR_VARIABLE: next_plan})
    assert not _taskless_request_anchor_changed(
        temp_db,
        session_id=session.id,
        prior_round_context=context,
    )
