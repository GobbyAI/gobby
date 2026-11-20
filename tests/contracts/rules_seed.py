"""Domain state seeded before a rule-pass case's rules.

A case's ``seed`` mapping names the rows the engine reads besides the rules: the session
row, synthetic agent definitions and a step instance. This module owns the identity
constants every recorder module shares, so it never imports ``rules_recorder``.

Every seeded definition is synthetic and written in the case file. Agent definitions are
installed globally through ``AgentDefinitionManager.upsert_with_steps``, and a step instance
is built with ``build_step_instance`` and saved through ``AgentStepInstanceManager``. A step
instance may store a ``snapshot_step_workflow`` that differs from the installed definition's
``step_workflow``, so a case can show which of the two the engine enforces.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

SESSION_ID = "11111111-1111-4111-8111-111111111111"
SYNTHETIC_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
SYNTHETIC_USER_ID = "20000000-0000-4000-8000-0000000000a1"
SYNTHETIC_MACHINE_ID = "20000000-0000-4000-8000-0000000000aa"
SYNTHETIC_USER_EMAIL = "synthetic-user@corpus.invalid"
SYNTHETIC_USER_NAME = "Synthetic Corpus User"
SYNTHETIC_PASSWORD_HASH = "synthetic-password-hash"
SYNTHETIC_PROJECT_NAME = "synthetic-project"
SYNTHETIC_EXTERNAL_ID = "synthetic-external"
SEED_ORDER: tuple[str, ...] = ("session", "agent_definitions", "step_instance")
AGENT_SPEC_KEYS = frozenset({"name", "body", "step_workflow", "enabled"})


def seed_identity(db: PostgresHubDatabase) -> None:
    """Insert the synthetic user and machine rows (ON CONFLICT DO NOTHING)."""
    db.execute(
        "INSERT INTO users (id, email, name, password_hash) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (SYNTHETIC_USER_ID, SYNTHETIC_USER_EMAIL, SYNTHETIC_USER_NAME, SYNTHETIC_PASSWORD_HASH),
    )
    db.execute(
        "INSERT INTO machines (id, owner_user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (SYNTHETIC_MACHINE_ID, SYNTHETIC_USER_ID),
    )


def _refuse(case_id: str, key: str, why: str) -> ValueError:
    return ValueError(f"{case_id}: seed {key!r} {why}")


def _step_names(workflow: Any) -> list[str]:
    if not isinstance(workflow, Mapping):
        return []
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        return []
    return [str(step.get("name")) for step in steps if isinstance(step, Mapping)]


def _validate_agent_definitions(case_id: str, specs: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(specs, list) or not specs:
        raise _refuse(case_id, "agent_definitions", "must be a non-empty list")
    by_name: dict[str, Mapping[str, Any]] = {}
    for spec in specs:
        if not isinstance(spec, Mapping):
            raise _refuse(case_id, "agent_definitions", "holds an entry that is not a mapping")
        unknown = sorted(set(spec) - AGENT_SPEC_KEYS)
        if unknown:
            raise _refuse(case_id, "agent_definitions", f"has unknown key(s) {unknown}")
        name = spec.get("name")
        if not isinstance(name, str) or not name.startswith("synthetic-"):
            raise _refuse(case_id, "agent_definitions", f"name {name!r} is not synthetic")
        if not isinstance(spec.get("body"), Mapping):
            raise _refuse(case_id, "agent_definitions", f"{name!r} has no body mapping")
        by_name[name] = spec
    return by_name


def validate_seed(case_id: str, seed: Mapping[str, Any] | None) -> None:
    """Raise ``ValueError`` naming the case and the key of a malformed seed.

    Refused: a key with no seeder; a step instance without the session or the agent
    definitions; a step instance whose agent is not seeded; a seeded definition without a
    step named ``current_step``; a ``snapshot_step_workflow`` without that step.
    """
    if seed is None:
        return
    if not isinstance(seed, Mapping):
        raise _refuse(case_id, "seed", "must be a mapping")
    unknown = sorted(key for key in seed if key not in SEEDERS)
    if unknown:
        raise _refuse(case_id, ", ".join(unknown), "has no seeder")
    if "session" in seed and seed["session"] is not True:
        raise _refuse(case_id, "session", "must be true")
    definitions: dict[str, Mapping[str, Any]] = {}
    if "agent_definitions" in seed:
        definitions = _validate_agent_definitions(case_id, seed["agent_definitions"])
    if "step_instance" not in seed:
        return
    instance = seed["step_instance"]
    if not isinstance(instance, Mapping):
        raise _refuse(case_id, "step_instance", "must be a mapping")
    if "session" not in seed:
        raise _refuse(case_id, "session", "is required by step_instance")
    if "agent_definitions" not in seed:
        raise _refuse(case_id, "agent_definitions", "is required by step_instance")
    agent = instance.get("agent")
    if agent not in definitions:
        raise _refuse(case_id, "step_instance", f"agent {agent!r} is not a seeded definition")
    current = instance.get("current_step")
    if current not in _step_names(definitions[str(agent)].get("step_workflow")):
        raise _refuse(
            case_id, "current_step", f"{current!r} is not a step of {agent!r}'s step_workflow"
        )
    snapshot = instance.get("snapshot_step_workflow")
    if snapshot is not None and current not in _step_names(snapshot):
        raise _refuse(
            case_id, "current_step", f"{current!r} is not a step of snapshot_step_workflow"
        )


def assert_no_dropped_keys(raw: Any, dumped: Any, path: str = "") -> None:
    """Raise ``ValueError`` when a key of ``raw`` is absent from ``dumped`` at the same path.

    Dicts are compared by key and lists by index, recursively, so a key the model silently
    ignores fails the seed instead of seeding a definition that differs from the case.
    """
    if isinstance(raw, Mapping):
        if not isinstance(dumped, Mapping):
            raise ValueError(
                f"seeded value at {path or '<root>'} is not a mapping after validation"
            )
        for key, value in raw.items():
            where = f"{path}.{key}" if path else str(key)
            if key not in dumped:
                raise ValueError(f"seeded key {where} was dropped by validation")
            assert_no_dropped_keys(value, dumped[key], where)
    elif isinstance(raw, list):
        if not isinstance(dumped, list) or len(dumped) != len(raw):
            raise ValueError(f"seeded list at {path or '<root>'} changed length in validation")
        for index, (item, kept) in enumerate(zip(raw, dumped, strict=True)):
            assert_no_dropped_keys(item, kept, f"{path}[{index}]")


def _validated_step_workflow(workflow: Mapping[str, Any]) -> AgentStepWorkflowBody:
    body = AgentStepWorkflowBody.model_validate(dict(workflow))
    assert_no_dropped_keys(workflow, body.model_dump(), "step_workflow")
    return body


def seed_session(db: PostgresHubDatabase, spec: bool) -> None:
    """Insert the identity rows, the synthetic project and the SESSION_ID session row."""
    if spec is not True:
        return
    seed_identity(db)
    db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (SYNTHETIC_PROJECT_ID, SYNTHETIC_PROJECT_NAME),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (
            SESSION_ID,
            SYNTHETIC_EXTERNAL_ID,
            SYNTHETIC_MACHINE_ID,
            "claude",
            SYNTHETIC_PROJECT_ID,
            "active",
        ),
    )


def seed_agent_definitions(db: PostgresHubDatabase, specs: Sequence[Mapping[str, Any]]) -> None:
    """Install each synthetic agent definition globally, with its step workflow."""
    manager = AgentDefinitionManager(db)
    for spec in specs:
        name = str(spec["name"])
        body = {"name": name, **dict(spec["body"])}
        assert_no_dropped_keys(body, AgentDefinitionBody.model_validate(body).model_dump())
        workflow = spec.get("step_workflow")
        if workflow is not None:
            _validated_step_workflow(workflow)
        manager.upsert_with_steps(
            name,
            body,
            dict(workflow) if workflow is not None else None,
            enabled=bool(spec.get("enabled", True)),
        )


def seed_step_instance(
    db: PostgresHubDatabase,
    spec: Mapping[str, Any],
    *,
    session_id: str,
    definitions: Sequence[Mapping[str, Any]],
) -> None:
    """Save the step instance of a seeded definition for ``session_id``.

    The stored snapshot is ``snapshot_step_workflow`` when the spec has one, else the
    definition's own ``step_workflow``; the installed definition keeps its own.
    """
    agent = str(spec["agent"])
    definition = next(item for item in definitions if item["name"] == agent)
    workflow = spec.get("snapshot_step_workflow") or definition["step_workflow"]
    _validated_step_workflow(workflow)
    body = AgentDefinitionBody.model_validate(
        {"name": agent, **dict(definition["body"]), "step_workflow": dict(workflow)}
    )
    instance = build_step_instance(
        body,
        session_id=session_id,
        step_workflow_id=None,
        current_step=str(spec["current_step"]),
    )
    AgentStepInstanceManager(db).save(instance)


def _seed_session_entry(db: PostgresHubDatabase, seed: Mapping[str, Any]) -> None:
    seed_session(db, bool(seed["session"]))


def _seed_agent_definitions_entry(db: PostgresHubDatabase, seed: Mapping[str, Any]) -> None:
    seed_agent_definitions(db, seed["agent_definitions"])


def _seed_step_instance_entry(db: PostgresHubDatabase, seed: Mapping[str, Any]) -> None:
    seed_step_instance(
        db,
        seed["step_instance"],
        session_id=SESSION_ID,
        definitions=seed["agent_definitions"],
    )


# Each seeder takes the whole seed mapping, so one key's seeder can read another key.
SEEDERS: dict[str, Callable[[PostgresHubDatabase, Mapping[str, Any]], None]] = {
    "session": _seed_session_entry,
    "agent_definitions": _seed_agent_definitions_entry,
    "step_instance": _seed_step_instance_entry,
}


def apply_case_seed(db: PostgresHubDatabase, seed: Mapping[str, Any] | None) -> None:
    """Run the seeder of each key the seed carries, in SEED_ORDER."""
    if not seed:
        return
    for key in SEED_ORDER:
        if key in seed:
            SEEDERS[key](db, seed)
