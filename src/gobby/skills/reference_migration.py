"""Migrate instruction lists, leaving user-owned requirements and runtime history intact.

Definition ownership follows normal template sync: global, installed, and tagged
gobby. Runtime session values and task requirements have no equivalent provenance;
they are reported for explicit correction, never rewritten based on agent identity.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from gobby.skills.capability_catalog import CapabilityCatalog
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase

_REQUIREMENT_FIELDS = frozenset({"required_skills", "additional_skills"})


@dataclass
class ReferenceMigrationResult:
    updated: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _requirement_list(
    value: Any,
    *,
    location: str,
    owned: bool,
    catalog: CapabilityCatalog,
    result: ReferenceMigrationResult,
) -> Any:
    """Validate one instruction field and replace exact retired names only."""
    diagnostics = result.errors if owned else result.warnings
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        diagnostics.append(f"{location}: expected a list of instruction identifiers; preserved")
        return value
    try:
        for item in value:
            parse_instruction_requirement(item)
    except ValueError as exc:
        diagnostics.append(f"{location}: invalid instruction identifier ({exc}); preserved")
        return value
    replacements = {
        item: catalog.folded_skills[item] for item in value if item in catalog.folded_skills
    }
    if not replacements:
        return value
    if not owned:
        advice = "; ".join(f"{old} -> {new}" for old, new in replacements.items())
        result.warnings.append(f"{location}: user-owned requirement preserved; replace {advice}")
        return value
    return list(dict.fromkeys(replacements.get(item, item) for item in value))


def _variables(
    variables: dict[str, Any],
    *,
    location: str,
    owned: bool,
    catalog: CapabilityCatalog,
    result: ReferenceMigrationResult,
) -> None:
    for name in _REQUIREMENT_FIELDS & variables.keys():
        variables[name] = _requirement_list(
            variables[name],
            location=f"{location}.{name}",
            owned=owned,
            catalog=catalog,
            result=result,
        )


def migrate_instruction_requirements(
    db: HubDatabase, catalog: CapabilityCatalog
) -> ReferenceMigrationResult:
    """Convert active definition fields through typed managers, one transaction per row.

    Failed rows roll back independently and stop skill retirement in the caller.
    Retrying is safe: exact references are unchanged and converted lists deduplicate.
    No prose, expression, transcript, loaded-instruction ledger, or task state is edited.
    """
    result = ReferenceMigrationResult()
    agents = AgentDefinitionManager(db)
    rules = RuleDefinitionManager(db)
    defaults = SessionVariableDefaultManager(db)
    for table, manager in (
        ("agent_definitions", agents),
        ("rule_definitions", rules),
        ("session_variable_defaults", defaults),
    ):
        for candidate in manager.list_all():
            location = f"{table}[{candidate.id}] ({candidate.name})"
            changed = False
            try:
                with db.transaction() as txn:
                    # Read ownership and content after locking, not from the
                    # inventory. Agent child edits also need their own row lock.
                    locked = txn.execute(
                        f"SELECT id FROM {table} WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                        (candidate.id,),
                    ).fetchone()
                    if locked is None:
                        continue
                    current = manager.get(candidate.id)
                    owned = (
                        current.project_id is None
                        and current.source == "installed"
                        and "gobby" in (current.tags or [])
                    )
                    if table == "agent_definitions":
                        txn.execute(
                            "SELECT id FROM agent_step_workflows "
                            "WHERE agent_definition_id = %s FOR UPDATE",
                            (candidate.id,),
                        ).fetchone()
                        agent = agents.get(candidate.id)
                        child = agent.definition_json.get("step_workflow")
                        if not isinstance(child, dict):
                            continue
                        updated_child = deepcopy(child)
                        variables = updated_child.get("variables")
                        if isinstance(variables, dict):
                            _variables(
                                variables,
                                location=f"{location}.step_workflow.variables",
                                owned=owned,
                                catalog=catalog,
                                result=result,
                            )
                        if updated_child != child:
                            agents.set_step_workflow(candidate.id, updated_child)
                            changed = True
                    elif table == "rule_definitions":
                        rule = rules.get(candidate.id)
                        body = deepcopy(rule.definition_json)
                        effects = body.get("effects", [])
                        if not isinstance(effects, list):
                            continue
                        for index, effect in enumerate(effects):
                            if (
                                isinstance(effect, dict)
                                and effect.get("type") == "set_variable"
                                and effect.get("variable") in _REQUIREMENT_FIELDS
                                and "value" in effect
                            ):
                                effect["value"] = _requirement_list(
                                    effect["value"],
                                    location=f"{location}.effects[{index}].value",
                                    owned=owned,
                                    catalog=catalog,
                                    result=result,
                                )
                            elif (
                                isinstance(effect, dict)
                                and effect.get("type") == "load_skill"
                                and isinstance(effect.get("skill"), str)
                            ):
                                [effect["skill"]] = _requirement_list(
                                    [effect["skill"]],
                                    location=f"{location}.effects[{index}].skill",
                                    owned=owned,
                                    catalog=catalog,
                                    result=result,
                                )
                        if body != rule.definition_json:
                            rules.update_from_sync(candidate.id, definition_json=body)
                            changed = True
                    elif current.name in _REQUIREMENT_FIELDS:
                        default = defaults.get(candidate.id)
                        converted = _requirement_list(
                            default.default_value,
                            location=f"{location}.default_value",
                            owned=owned,
                            catalog=catalog,
                            result=result,
                        )
                        if converted != default.default_value:
                            defaults.update_from_sync(candidate.id, default_value=converted)
                            changed = True
                if changed:
                    result.updated += 1
            except Exception as exc:
                result.errors.append(f"{location}: reference migration failed: {exc}; retry sync")

    # These fields are explicit user/session state, without template provenance.
    # Read-only diagnostics deliberately include no transcript or historical text.
    for row in db.fetchall("SELECT session_id, variables FROM session_variables"):
        location = f"session_variables[{row['session_id']}]"
        try:
            variables = row["variables"]
            if isinstance(variables, str):
                variables = json.loads(variables)
            if isinstance(variables, dict):
                _variables(
                    variables,
                    location=location,
                    owned=False,
                    catalog=catalog,
                    result=result,
                )
        except ValueError as exc:
            result.warnings.append(f"{location}: malformed variables ({exc}); preserved")
    for row in db.fetchall(
        "SELECT id, session_id, variables, snapshot_json FROM agent_step_instances WHERE enabled = TRUE"
    ):
        location = f"agent_step_instances[{row['id']}] (session {row['session_id']})"
        for field_name in ("variables", "snapshot_json"):
            try:
                payload = row[field_name]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if field_name == "snapshot_json" and isinstance(payload, dict):
                    payload = payload.get("variables")
                if isinstance(payload, dict):
                    _variables(
                        payload,
                        location=f"{location}.{field_name}",
                        owned=False,
                        catalog=catalog,
                        result=result,
                    )
            except ValueError as exc:
                result.warnings.append(
                    f"{location}.{field_name}: malformed payload ({exc}); preserved"
                )
    # Closed tasks never load requirements; reopening makes them visible again.
    for row in db.fetchall(
        "SELECT id, additional_skills FROM tasks "
        "WHERE additional_skills IS NOT NULL AND closed_at IS NULL"
    ):
        location = f"tasks[{row['id']}].additional_skills"
        try:
            value = row["additional_skills"]
            if isinstance(value, str):
                value = json.loads(value)
            _requirement_list(value, location=location, owned=False, catalog=catalog, result=result)
        except ValueError as exc:
            result.warnings.append(f"{location}: malformed requirements ({exc}); preserved")
    return result
