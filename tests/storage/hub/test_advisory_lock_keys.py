"""Pin the language-independent advisory-lock key contract."""

from collections.abc import Callable
from dataclasses import dataclass, is_dataclass
from typing import ClassVar

import pytest

from gobby.storage.hub import protocol
from gobby.storage.hub.postgres_pool import advisory_lock_keys

pytestmark = pytest.mark.unit

# Use the same resource value across namespaces so collisions cannot hide behind
# different fixture identifiers. Transfer deliberately reuses variable-row keys.
KEY_CASES: tuple[tuple[protocol.LockTarget, tuple[str, ...]], ...] = (
    (protocol.MainCheckoutLanding("resource"), ("main_checkout_landing:resource",)),
    (protocol.BuildDryRunMutation("resource"), ("build_dry_run:resource",)),
    (protocol.CronRunAdmission(), ("cron_run_admission",)),
    (protocol.AgentCapAdmission("resource"), ("agent_cap_admission:resource",)),
    (protocol.AgentTaskClaimMutation("resource"), ("agent_task_claim:resource",)),
    (
        protocol.ReviewLearningPatternMutation("resource", "pattern"),
        ("review_learning_pattern:resource:pattern",),
    ),
    (protocol.TaskSeqAllocation("resource"), ("task_seq:resource",)),
    (protocol.ExpansionApplyMutation("resource"), ("expansion_apply:resource",)),
    (protocol.TaskDependencyMutation(), ("task_dependency_mutation",)),
    (protocol.StageReviewApprovalMutation("resource"), ("stage_review_approval:resource",)),
    (protocol.StageReviewRejectionMutation("resource"), ("stage_review_rejection:resource",)),
    (protocol.DispatchMutexRow("resource"), ("dispatch_mutex:resource",)),
    (
        protocol.IntegrationWorkspaceMutex("resource"),
        ("integration_workspace_mutex:resource",),
    ),
    (protocol.TaskSubtreeCascade("resource"), ("task_subtree_cascade:resource",)),
    (protocol.TaskLifecycleMutation("resource"), ("task_lifecycle:resource",)),
    (
        protocol.PlanReviewEvidenceMutation("resource", "plan.md"),
        ("plan_review_evidence:resource:plan.md",),
    ),
    (
        protocol.WebChatSessionBootstrap("resource", "machine", "source", "project", "type"),
        ("web_chat_session:resource|machine|source|project|type",),
    ),
    (
        protocol.IsolationRegistryReconciliation("resource"),
        ("isolation_registry_reconciliation:resource",),
    ),
    (
        protocol.SessionRegistration("resource", "source", "type"),
        ("session_register:resource|source|type",),
    ),
    (protocol.SessionLineageMutation(), ("session_lineage_mutation",)),
    (protocol.SessionRecoveryByProject("resource"), ("session_recovery:resource",)),
    (protocol.SessionSeqMutation("resource"), ("session_seq:resource",)),
    (protocol.SystemSessionBootstrap(), ("system_session_bootstrap",)),
    (protocol.WorkflowDefinitionMutation("resource"), ("workflow_definition:resource",)),
    (protocol.AgentStepInstanceMutation("resource"), ("agent_step_instance:resource",)),
    (protocol.ChatAttachmentMutation(), ("chat_attachment_mutation",)),
    (protocol.SessionVariableMutation("resource"), ("session_variable:resource",)),
    (
        protocol.SessionVariableTransfer(("second", "resource", "second")),
        ("session_variable:resource", "session_variable:second"),
    ),
)


@pytest.mark.parametrize(
    ("lock", "expected"), KEY_CASES, ids=[type(lock).__name__ for lock, _ in KEY_CASES]
)
def test_lock_target_has_explicit_key(lock: protocol.LockTarget, expected: tuple[str, ...]) -> None:
    assert advisory_lock_keys(lock) == expected


def test_key_table_covers_every_lock_target_with_unique_namespaces() -> None:
    # LockTarget is a structural Protocol: implementations do not inherit it.
    defined_targets: set[type[object]] = {
        value
        for value in vars(protocol).values()
        if isinstance(value, type) and is_dataclass(value) and hasattr(value, "PRIORITY")
    }
    covered_targets: set[type[object]] = {type(lock) for lock, _ in KEY_CASES}
    assert covered_targets == defined_targets

    keys = [
        key
        for lock, _ in KEY_CASES
        if not isinstance(lock, protocol.SessionVariableTransfer)
        for key in advisory_lock_keys(lock)
    ]
    assert len(keys) == len(set(keys)), "Distinct lock namespaces must not collide"
    assert all("gobby.storage." not in key for key in keys), "No module-path/repr fallback"


@dataclass(frozen=True)
class UnmappedLockTarget(protocol.LockTarget):
    PRIORITY: ClassVar[int] = 1000
    resource_id: str = "resource"


def test_unmapped_lock_target_raises() -> None:
    with pytest.raises((TypeError, ValueError), match="UnmappedLockTarget"):
        advisory_lock_keys(UnmappedLockTarget())


@pytest.mark.parametrize(
    "target_type",
    (
        protocol.TaskSeqAllocation,
        protocol.ExpansionApplyMutation,
        protocol.SessionVariableMutation,
        protocol.WorkflowDefinitionMutation,
        protocol.AgentStepInstanceMutation,
    ),
)
def test_resource_scoped_keys_distinguish_resources(
    target_type: Callable[[str], protocol.LockTarget],
) -> None:
    assert advisory_lock_keys(target_type("first")) != advisory_lock_keys(target_type("second"))


def test_variable_transfer_locks_the_same_rows_in_sorted_unique_order() -> None:
    assert advisory_lock_keys(protocol.SessionVariableTransfer(("b", "a", "b"))) == (
        *advisory_lock_keys(protocol.SessionVariableMutation("a")),
        *advisory_lock_keys(protocol.SessionVariableMutation("b")),
    )


def test_empty_variable_transfer_has_no_rows_to_lock() -> None:
    assert advisory_lock_keys(protocol.SessionVariableTransfer(())) == ()
