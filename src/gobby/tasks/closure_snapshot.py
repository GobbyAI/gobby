"""Capture task, evidence and effective installed policy in the same transaction."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from gobby.releases.activation import project_checkout_root
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import claimant_sessions
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskStaleStateError
from gobby.tasks.attestations import AttestationError, list_attestations
from gobby.tasks.close_receipts import list_close_receipts
from gobby.tasks.closure_policy import ClosureDecision, ClosureSnapshot, evaluate_closure_policy
from gobby.tasks.landing_versions import verify_landing_transform
from gobby.tasks.lifecycle_access import resolve_lifecycle_task
from gobby.tasks.subtree_snapshot import capture_subtree_facts
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import (
    is_operator_session,
    resolve_agent,
    session_agent_definition,
)
from gobby.workflows.state_manager import SessionVariableManager

_JSON_OBJECT = TypeAdapter(dict[str, Any])
_JSON_POLICY = TypeAdapter(dict[str, JsonValue])


def _admitted_landing_receipts(db: HubDatabase, task: Task) -> list[dict[str, JsonValue]]:
    """Version receipts carry evidence only after their exact Git trees reproduce."""
    admitted: list[dict[str, JsonValue]] = []
    for receipt in list_close_receipts(db, task.id):
        facts: dict[str, JsonValue] = dict(receipt.facts)
        facts.pop("verified_version_transform", None)
        if "version_bump_commit" in facts:
            required = (
                "reviewed_sha",
                "integration_base",
                "version_source_commit",
                "version_bump_commit",
            )
            values = {key: facts.get(key) for key in required}
            if (
                facts.get("branch") != "0.5.0"
                or values["reviewed_sha"] != receipt.commit_sha
                or values["version_bump_commit"] != facts.get("landed_tip")
                or not all(isinstance(value, str) for value in values.values())
                or not verify_landing_transform(
                    Path(project_checkout_root(db, task.project_id)),
                    reviewed=str(values["reviewed_sha"]),
                    base=str(values["integration_base"]),
                    source=str(values["version_source_commit"]),
                    bump=str(values["version_bump_commit"]),
                )
            ):
                continue
            if values["version_source_commit"] == values["reviewed_sha"]:
                facts["verified_version_transform"] = True
        admitted.append(
            {"commit_sha": receipt.commit_sha, "recorded_at": receipt.recorded_at, "facts": facts}
        )
    return admitted


def installed_project_policy(
    rules: list[RuleDefinitionRow], *, provenance: list[RuleDefinitionRow] | None = None
) -> dict[str, JsonValue] | None:
    """Read a single authoritative policy value from enabled installed effects.

    Session variables are projections controlled by hook delivery timing. They
    cannot authorize security decisions. No configured policy contributes an
    empty path floor; malformed or conflicting writers fail closed.
    """
    registry = provenance if provenance is not None else rules
    writers = [
        rule
        for rule in registry
        if any(
            isinstance(effect, dict)
            and effect.get("type") == "set_variable"
            and effect.get("variable") == "project_policy"
            for effect in rule.definition_json.get("effects", [])
        )
    ]
    installed_names = {rule.name for rule in writers if rule.source == "installed"}
    if any(rule.source != "installed" for rule in writers) or any(
        rule.source != "installed" and rule.name in installed_names for rule in registry
    ):
        return None
    policies = [
        effect.get("value")
        for rule in rules
        if rule.enabled
        for effect in rule.definition_json.get("effects", [])
        if isinstance(effect, dict)
        and effect.get("type") == "set_variable"
        and effect.get("variable") == "project_policy"
    ]
    if not policies:
        return {}
    if any(not isinstance(value, dict) for value in policies):
        return None
    if any(value != policies[0] for value in policies):
        return None
    try:
        policy = _JSON_POLICY.validate_python(policies[0])
    except ValidationError:
        return None
    paths = policy.get("security_paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        return None
    return policy


@dataclass(frozen=True)
class PreparedClosure:
    task: Task
    agent: AgentDefinitionBody
    snapshot: ClosureSnapshot
    decision: ClosureDecision


class TaskClosurePolicyError(ValueError):
    def __init__(self, task_id: str, decision: ClosureDecision) -> None:
        self.task_id = task_id
        self.decision = decision
        super().__init__("Task closure blocked: " + "; ".join(decision.blockers))


def prepare_closure(
    db: HubDatabase,
    *,
    task_id: str,
    session_id: str | None,
    reason: str = "done",
    facts: dict[str, JsonValue] | None = None,
    expected_updated_at: datetime | None = None,
    prospective_commits: list[str] | None = None,
    event: Literal["task_close", "task_land"] = "task_close",
    derive_facts: Callable[[Task, AgentDefinitionBody], dict[str, JsonValue]] | None = None,
) -> PreparedClosure:
    """Retain all locks through an enclosing final-close transaction, if present.

    Preview calls release them when the snapshot is returned. Final close calls
    use the same function inside their mutation transaction, preventing policy
    changes, evidence appends and child attachment during the decision/write.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = %s FOR NO KEY UPDATE", (task_id,)
        ).fetchone()
        if row is None:
            raise AttestationError("closure task does not exist")
        task = Task.from_row(row)
        if expected_updated_at is not None and task.updated_at != expected_updated_at:
            raise TaskStaleStateError(task_id)
        if session_id is not None:
            conn.execute(
                "SELECT id FROM sessions WHERE id = %s FOR SHARE", (session_id,)
            ).fetchone()
            conn.execute(
                "SELECT session_id FROM session_variables WHERE session_id = %s FOR SHARE",
                (session_id,),
            ).fetchone()
        # A row lock cannot prevent a newly inserted project override. These
        # brief shared table locks also serialize enable/disable and overrides.
        conn.execute("LOCK TABLE agent_definitions, rule_definitions IN SHARE MODE")
        session = SessionManager(db).get(session_id) if session_id is not None else None
        if session_id is not None:
            if session is None:
                raise AttestationError("task policy requires an authenticated session")
            resolve_lifecycle_task(
                LocalTaskManager(db),
                task.id,
                project_id=session.project_id,
                caller_session_id=session.id,
                sessions=SessionManager(db),
            )
        definition = session_agent_definition(db, session) if session is not None else None
        if definition is None:
            if session is not None:
                role = (
                    SessionVariableManager(db)
                    .get_variable_subset(session.id, ("_agent_type",))
                    .get("_agent_type")
                )
                if not is_operator_session(session, role):
                    raise AttestationError("closure requires an enabled installed agent definition")
            # Internal system operations and unseated operators use the default
            # selector. Mandatory rules still apply; absence of a session never
            # suppresses policy evaluation. Agent transports require a session.
            fallback = resolve_agent("default", db, project_id=task.project_id)
            if fallback is None:
                raise AttestationError("default closure definition is unavailable")
            definition = (
                fallback,
                hashlib.sha256(
                    json.dumps(fallback.model_dump(mode="json"), sort_keys=True).encode()
                ).hexdigest(),
            )
        agent, digest = definition
        child_ids = {
            str(child["id"])
            for child in conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = %s", (task_id,)
            ).fetchall()
        }
        children = conn.execute(
            "SELECT id, title, closed_at FROM tasks WHERE parent_task_id = %s "
            "ORDER BY id FOR SHARE SKIP LOCKED",
            (task_id,),
        ).fetchall()
        # A closing child already owns its row before cascading to its parent.
        # Never wait for a sibling here: it may be waiting for this parent lock.
        # An incomplete snapshot asks for a retry without aborting the transaction.
        if {str(child["id"]) for child in children} != child_ids:
            raise TaskStaleStateError(task_id)
        child_data = [
            {
                "id": str(child["id"]),
                "title": child["title"],
                "closed": child["closed_at"] is not None,
            }
            for child in children
        ]
        task_data = _JSON_OBJECT.dump_python(task.to_dict(), mode="json")
        if prospective_commits is not None:
            task_data["commits"] = list(prospective_commits)
        rule_store = RuleDefinitionManager(db)
        rules = rule_store.list_effective(task.project_id)
        project_policy = installed_project_policy(
            rules, provenance=rule_store.list_all(project_id=task.project_id)
        )
        snapshot = ClosureSnapshot(
            event=event,
            task=task_data,
            caller={
                "session_id": session_id,
                "definition": agent.name,
                "definition_hash": digest,
                "capabilities": list(agent.capabilities),
                "project_id": session.project_id if session is not None else None,
                "machine_id": session.machine_id if session is not None else None,
            },
            reason=reason,
            attestations=tuple(list_attestations(db, task_id)),
            facts={
                "closure_intent": "explicit",
                **(facts or {}),
                **(derive_facts(task, agent) if derive_facts is not None else {}),
                **capture_subtree_facts(db, conn, task_id),
                "children": child_data,
                "children_closed": all(child["closed"] for child in child_data),
                "claimants": sorted(claimant_sessions(conn, task_id)),
                "project_policy": project_policy or {},
                "project_policy_available": project_policy is not None,
                "landing_receipts": _admitted_landing_receipts(db, task),
            },
        )
        decision = evaluate_closure_policy(snapshot, agent=agent, rules=rules)
        return PreparedClosure(task, agent, snapshot, decision)
