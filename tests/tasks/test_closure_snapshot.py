"""Preview and final close consume an identical authenticated policy snapshot."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from typing import Any

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import AttestationSubmission, list_attestations, record_attestation
from gobby.tasks.closure_snapshot import TaskClosurePolicyError, prepare_closure
from tests.fixtures.agent_sessions import AgentSessions as _Seats

pytestmark = pytest.mark.unit


def test_snapshot_uses_installed_policy_instead_of_caller_variables(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    project = sample_project["id"]
    author = _Seats(temp_db, project).add("policy-reader", [])
    task = LocalTaskManager(temp_db).create_task(project, "Map protected paths")
    SessionVariableManager(temp_db).set_variable(author, "project_policy", {})
    policy = {"security_paths": ["src/locks/"]}
    definitions = RuleDefinitionManager(temp_db)
    definitions.create(
        "arbitrary-installed-policy",
        {
            "event": "session_start",
            "effects": [{"type": "set_variable", "variable": "project_policy", "value": policy}],
        },
        project_id=project,
    )
    prepared = prepare_closure(temp_db, task_id=task.id, session_id=author)
    assert prepared.snapshot.facts["project_policy"] == policy
    assert prepared.snapshot.facts["project_policy_available"] is True
    assert prepared.snapshot.facts["landing_receipts"] == []


def test_missing_installed_policy_has_an_empty_security_floor(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project = sample_project["id"]
    author = _Seats(temp_db, project).add(
        "policy-reader",
        [],
        definition_fields={"workflows": {"rule_selectors": {"include": ["policy-required"]}}},
    )
    task = LocalTaskManager(temp_db).create_task(project, "Map protected paths")
    RuleDefinitionManager(temp_db).create(
        "policy-required",
        {
            "event": "task_close",
            "when": "not facts.get('project_policy_available')",
            "effects": [{"type": "block", "reason": "Installed policy unavailable"}],
        },
        project_id=project,
    )
    prepared = prepare_closure(temp_db, task_id=task.id, session_id=author)
    assert prepared.decision.allowed
    assert prepared.snapshot.facts["project_policy_available"] is True
    assert prepared.snapshot.facts["project_policy"] == {}


def test_snapshot_tracks_policy_and_evidence_without_a_repository(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "a-free-standing-author",
        [],
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": ["tag:closure"]}},
        },
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id, "Bake a cake", category="manual", validation_criteria="Cake is implemented."
    )
    definitions = RuleDefinitionManager(temp_db)
    rule = definitions.create(
        "arbitrary-implementation-rule",
        {
            "event": "task_close",
            "when": "latest_attestation('implementation').get('result') != 'approved'",
            "effects": [{"type": "block", "reason": "Implementation evidence is missing."}],
        },
        project_id=project_id,
        tags=["closure"],
    )
    missing = prepare_closure(temp_db, task_id=task.id, session_id=author)
    assert not missing.decision.allowed
    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="implementation", result="approved", summary="Baked the cake."
        ),
    )
    complete = prepare_closure(temp_db, task_id=task.id, session_id=author)
    assert complete.decision.allowed
    assert complete.snapshot.caller["session_id"] == author
    assert complete.decision.policy_revision == missing.decision.policy_revision
    assert complete.decision.snapshot_revision != missing.decision.snapshot_revision
    assert prepare_closure(temp_db, task_id=task.id, session_id=author) == complete
    definitions.update(
        rule.id,
        definition_json={
            "event": "task_close",
            "when": "True",
            "effects": [{"type": "block", "reason": "Operator paused closures."}],
        },
    )
    changed = prepare_closure(temp_db, task_id=task.id, session_id=author)
    assert not changed.decision.allowed
    assert changed.decision.policy_revision != complete.decision.policy_revision
    assert LocalTaskManager(temp_db).get_task(task.id).closed_at is None


def test_storage_close_evaluates_selected_policy_and_accepts_non_git_implementation(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "solo-baker",
        [],
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": ["tag:closure"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id, "Bake cake", category="manual", validation_criteria="Cake is baked."
    )
    RuleDefinitionManager(temp_db).create(
        "a-selected-requirement",
        {
            "event": "task_close",
            "when": "latest_attestation('implementation').get('result') != 'approved'",
            "effects": [{"type": "block", "reason": "Implementation evidence is missing."}],
        },
        project_id=project_id,
        tags=["closure"],
    )
    with pytest.raises(TaskClosurePolicyError, match="Implementation evidence"):
        manager.close_task(task.id, closed_in_session_id=author)
    assert manager.get_task(task.id).closed_at is None
    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="implementation", result="approved", summary="Cake is baked."
        ),
    )
    manager.close_task(task.id, closed_in_session_id=author)
    assert manager.get_task(task.id).closed_at is not None
    assert manager.get_task(task.id).commits is None


def test_storage_has_no_implicit_workflow_requirements_without_selected_rules(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add("unrestricted-synthetic-seat", [])
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id, "Parent", validation_criteria="Policy decides closure."
    )
    child = manager.create_task(
        project_id,
        "Child",
        parent_task_id=parent.id,
        validation_criteria="Policy may allow the parent to close first.",
    )
    manager.close_task(parent.id, closed_in_session_id=author)
    assert manager.get_task(parent.id).closed_at is not None
    assert manager.get_task(child.id).closed_at is None


def test_explicit_parent_close_retries_while_a_child_is_being_closed(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.storage.tasks import TaskStaleStateError

    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add("parent-race-author", [])
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(project_id, "Parent race", validation_criteria="Close safely.")
    child = manager.create_task(
        project_id, "Busy child", parent_task_id=parent.id, validation_criteria="Finish child."
    )
    child_locked, release_child = Event(), Event()

    def close_child() -> None:
        with temp_db.transaction() as conn:
            conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (child.id,)).fetchone()
            child_locked.set()
            assert release_child.wait(timeout=5), "Parent never released child."
            manager.close_task(child.id, closed_in_session_id=author)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(close_child)
        try:
            assert child_locked.wait(timeout=5), "Child did not acquire its lock."
            with temp_db.transaction() as conn:
                conn.execute("SET LOCAL statement_timeout = '2s'")
                with pytest.raises(TaskStaleStateError):
                    manager.close_task(parent.id, closed_in_session_id=author)
            assert manager.get_task(parent.id).closed_at is None
        finally:
            release_child.set()
        pending.result(timeout=5)
    assert manager.get_task(child.id).closed_at is not None
    assert manager.get_task(parent.id).closed_at is not None


@pytest.mark.parametrize(
    "event,enabled,when",
    [("task_close", True, "False"), ("task_close", False, "True"), ("before_tool", True, "True")],
    ids=["replacement", "disabled-override", "changed-event"],
)
def test_project_rule_override_replaces_global_before_filtering(
    temp_db: HubDatabase, sample_project: dict[str, Any], event: str, enabled: bool, when: str
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "project-policy-author",
        [],
        definition_fields={
            "workflows": {"rule_selectors": {"include": ["tag:closure"]}},
        },
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id, "Local policy task", validation_criteria="Use effective policy."
    )
    rules = RuleDefinitionManager(temp_db)
    rules.create(
        "scoped-closure-rule",
        {
            "event": "task_close",
            "when": "True",
            "effects": [{"type": "block", "reason": "Global policy."}],
        },
        tags=["closure"],
    )
    rules.create(
        "scoped-closure-rule",
        {
            "event": event,
            "when": when,
            "effects": [{"type": "block", "reason": "Project override."}],
        },
        project_id=project_id,
        enabled=enabled,
        tags=["closure"],
    )
    assert prepare_closure(temp_db, task_id=task.id, session_id=author).decision.allowed


@pytest.mark.parametrize("block_ancestor", [False, True])
def test_ancestor_closure_uses_policy_instead_of_task_type_or_criteria(
    temp_db: HubDatabase, sample_project: dict[str, Any], block_ancestor: bool
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "ancestor-policy-author",
        [],
        definition_fields={
            "workflows": {"rule_selectors": {"include": ["tag:closure"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(project_id, "Parent", validation_criteria="Policy controls this.")
    child = manager.create_task(
        project_id, "Child", parent_task_id=parent.id, validation_criteria="Complete the child."
    )
    RuleDefinitionManager(temp_db).create(
        "ancestor-policy",
        {
            "event": "task_close",
            "when": "facts.get('closure_intent') == 'ancestor'" if block_ancestor else "False",
            "effects": [{"type": "block", "reason": "Parent needs its own close."}],
        },
        project_id=project_id,
        tags=["closure"],
    )
    manager.close_task(child.id, closed_in_session_id=author)
    assert manager.get_task(child.id).closed_at is not None
    assert (manager.get_task(parent.id).closed_at is None) is block_ancestor


@pytest.mark.parametrize("change", ["evidence", "policy"])
def test_final_close_refuses_a_changed_preview_snapshot(
    temp_db: HubDatabase, sample_project: dict[str, Any], change: str
) -> None:
    from gobby.storage.tasks import TaskStaleStateError

    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "snapshot-author",
        [],
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": ["tag:closure"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id, "Snapshot task", validation_criteria="Use one snapshot.")
    rules = RuleDefinitionManager(temp_db)
    rule = rules.create(
        "revision-policy",
        {
            "event": "task_close",
            "when": "False",
            "effects": [{"type": "block", "reason": "Unused reason."}],
        },
        project_id=project_id,
        tags=["closure"],
    )
    preview = prepare_closure(temp_db, task_id=task.id, session_id=author)
    if change == "evidence":
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(
                kind="extra-check", result="pass", summary="New evidence."
            ),
        )
    else:
        rules.update(
            rule.id,
            definition_json={
                "event": "task_close",
                "when": "False",
                "effects": [{"type": "block", "reason": "New revision."}],
            },
        )
    with pytest.raises(TaskStaleStateError):
        manager.close_task(
            task.id,
            closed_in_session_id=author,
            expected_closure_revision=preview.decision.snapshot_revision,
        )
    assert manager.get_task(task.id).closed_at is None
    fresh = prepare_closure(temp_db, task_id=task.id, session_id=author)
    manager.close_task(
        task.id,
        closed_in_session_id=author,
        expected_closure_revision=fresh.decision.snapshot_revision,
    )
    assert manager.get_task(task.id).closed_at is not None


def test_commit_link_and_policy_close_are_one_transaction(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected policy rolls back the prospective link; a fresh preview can retry."""
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "atomic-author",
        [],
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": ["tag:atomic-close"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id, "Atomic close", validation_criteria="Link and close together."
    )
    RuleDefinitionManager(temp_db).create(
        "atomic-implementation",
        {
            "event": "task_close",
            "when": "latest_attestation('implementation').get('result') != 'pass'",
            "effects": [{"type": "block", "reason": "Implementation missing."}],
        },
        project_id=project_id,
        tags=["atomic-close"],
    )
    monkeypatch.setattr("gobby.utils.git.normalize_commit_sha", lambda sha, *, cwd: sha[:12])
    sha = "1234567890abcdef1234567890abcdef12345678"
    with pytest.raises(TaskClosurePolicyError, match="Implementation missing"):
        manager.close_task_with_commit(task.id, sha, closed_in_session_id=author)
    rejected = manager.get_task(task.id)
    assert rejected.commits is None
    assert rejected.closed_at is None
    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="implementation", result="pass", summary="Implemented atomically."
        ),
    )
    preview = prepare_closure(
        temp_db, task_id=task.id, session_id=author, prospective_commits=[sha[:12]]
    )
    closed = manager.close_task_with_commit(
        task.id,
        sha,
        closed_in_session_id=author,
        expected_closure_revision=preview.decision.snapshot_revision,
    )
    assert closed.closed_at is not None
    assert closed.commits == [sha[:12]]


def test_failed_commit_link_rolls_back_close_and_ancestor_cascade(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add("link-failure-author", [])
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id, "Link rollback parent", validation_criteria="Atomic family."
    )
    child = manager.create_task(
        project_id,
        "Link rollback child",
        parent_task_id=parent.id,
        validation_criteria="Atomic link.",
    )
    monkeypatch.setattr("gobby.utils.git.normalize_commit_sha", lambda sha, *, cwd: sha[:12])

    def fail_link(db: HubDatabase, task_id: str, commit_sha: str) -> bool:
        assert manager.get_task(task_id).closed_at is not None
        raise RuntimeError("Injected link failure.")

    monkeypatch.setattr("gobby.storage.tasks._transitions_facade._link_commit", fail_link)
    with pytest.raises(RuntimeError, match="Injected link failure"):
        manager.close_task_with_commit(
            child.id,
            "1234567890abcdef1234567890abcdef12345678",
            closed_in_session_id=author,
        )
    restored = manager.get_task(child.id)
    assert restored.closed_at is None
    assert restored.commits is None
    assert manager.get_task(parent.id).closed_at is None


def test_commit_link_refuses_stale_preview_without_leaving_a_link(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.storage.tasks import TaskStaleStateError

    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "stale-link-author",
        [],
        definition_fields={
            "task_attestation_writes": ["*"],
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id, "Stale link", validation_criteria="Reject stale snapshots."
    )
    preview = prepare_closure(
        temp_db, task_id=task.id, session_id=author, prospective_commits=["1234567890ab"]
    )
    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="new-fact", result="pass", summary="Changed since preview."
        ),
    )
    monkeypatch.setattr("gobby.utils.git.normalize_commit_sha", lambda sha, *, cwd: sha[:12])
    with pytest.raises(TaskStaleStateError):
        manager.close_task_with_commit(
            task.id,
            "1234567890abcdef1234567890abcdef12345678",
            closed_in_session_id=author,
            expected_closure_revision=preview.decision.snapshot_revision,
        )
    unchanged = manager.get_task(task.id)
    assert unchanged.commits is None
    assert unchanged.closed_at is None


@pytest.mark.parametrize("requires_commit", [True, False])
def test_prospective_commit_policy_matches_atomic_link_and_close(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    requires_commit: bool,
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "prospective-author",
        [],
        definition_fields={
            "workflows": {"rule_selectors": {"include": ["tag:prospective-close"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id, "Prospective close", validation_criteria="Preview proposed state."
    )
    RuleDefinitionManager(temp_db).create(
        "prospective-commit",
        {
            "event": "task_close",
            "when": "not task.commits" if requires_commit else "bool(task.commits)",
            "effects": [{"type": "block", "reason": "Commit policy refused."}],
        },
        project_id=project_id,
        tags=["prospective-close"],
    )
    monkeypatch.setattr("gobby.utils.git.normalize_commit_sha", lambda sha, *, cwd: sha[:12])
    sha = "1234567890abcdef1234567890abcdef12345678"
    preview = prepare_closure(
        temp_db, task_id=task.id, session_id=author, prospective_commits=[sha[:12]]
    )
    assert preview.decision.allowed is requires_commit
    assert preview.snapshot.task["commits"] == [sha[:12]]
    assert manager.get_task(task.id).commits is None
    if requires_commit:
        closed = manager.close_task_with_commit(
            task.id,
            sha,
            closed_in_session_id=author,
            expected_closure_revision=preview.decision.snapshot_revision,
        )
        assert closed.closed_at is not None
        assert closed.commits == [sha[:12]]
    else:
        with pytest.raises(TaskClosurePolicyError, match="Commit policy refused"):
            manager.close_task_with_commit(task.id, sha, closed_in_session_id=author)
        assert manager.get_task(task.id).closed_at is None
        assert manager.get_task(task.id).commits is None


@pytest.mark.parametrize("first_writer", ["close", "attestation"])
def test_append_and_close_serialize_on_the_same_task(
    temp_db: HubDatabase, sample_project: dict[str, Any], first_writer: str
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "concurrent-author",
        [],
        definition_fields={
            "task_attestation_writes": ["*"],
            "workflows": {"rule_selectors": {"include": ["tag:concurrent-close"]}},
        },
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id, "Concurrent close", validation_criteria="Serialize writers."
    )
    submission = AttestationSubmission(kind="implementation", result="pass", summary="Implemented.")
    started = Event()

    def append() -> None:
        started.set()
        record_attestation(temp_db, task_id=task.id, session_id=author, submission=submission)

    def close() -> None:
        started.set()
        manager.close_task(task.id, closed_in_session_id=author)

    if first_writer == "attestation":
        RuleDefinitionManager(temp_db).create(
            "concurrent-requirement",
            {
                "event": "task_close",
                "when": "latest_attestation('implementation').get('result') != 'pass'",
                "effects": [{"type": "block", "reason": "Implementation missing."}],
            },
            project_id=project_id,
            tags=["concurrent-close"],
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with temp_db.transaction() as conn:
            conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (task.id,)).fetchone()
            pending = pool.submit(append if first_writer == "close" else close)
            assert started.wait(timeout=5), "Concurrent writer did not start."
            if first_writer == "close":
                manager.close_task(task.id, closed_in_session_id=author)
            else:
                record_attestation(
                    temp_db, task_id=task.id, session_id=author, submission=submission
                )
        if first_writer == "close":
            with pytest.raises(ValueError, match="closed"):
                pending.result(timeout=5)
        else:
            pending.result(timeout=5)
    assert manager.get_task(task.id).closed_at is not None
    records = list_attestations(temp_db, task_id=task.id)
    assert len(records) == (0 if first_writer == "close" else 1)


def test_sibling_closes_do_not_deadlock_during_ancestor_policy_capture(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    author = _Seats(temp_db, project_id).add(
        "sibling-author",
        [],
        definition_fields={
            "workflows": {"rule_selectors": {"include": ["tag:children-close"]}},
        },
    )
    RuleDefinitionManager(temp_db).create(
        "children-complete",
        {
            "event": "task_close",
            "when": "not facts.children_closed",
            "effects": [{"type": "block", "reason": "Children remain open."}],
        },
        project_id=project_id,
        tags=["children-close"],
    )
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id, "Concurrent parent", validation_criteria="Children close."
    )
    children = [
        manager.create_task(
            project_id,
            f"Child {index}",
            parent_task_id=parent.id,
            validation_criteria="Child completed.",
        )
        for index in range(2)
    ]
    locked = Barrier(2, timeout=5)

    def close_child(task_id: str) -> None:
        with temp_db.transaction() as conn:
            conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
            locked.wait()
            manager.close_task(task_id, closed_in_session_id=author)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(close_child, child.id) for child in children]
        for result in pending:
            result.result(timeout=10)
    assert all(manager.get_task(child.id).closed_at is not None for child in children)
    assert manager.get_task(parent.id).closed_at is not None
