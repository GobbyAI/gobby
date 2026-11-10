"""Pure evaluation of selected YAML rules over one immutable closure snapshot."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestation_shapes import select_closure_rules
from gobby.tasks.attestations import TaskAttestation
from gobby.workflows.agent_models import AgentDefinitionBody, holds_capability
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator


class ClosureSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["task_close", "task_land"] = "task_close"
    task: dict[str, JsonValue]
    caller: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = "done"
    attestations: tuple[TaskAttestation, ...] = ()
    facts: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class ClosureDecision:
    allowed: bool
    blockers: tuple[str, ...]
    policy_revision: str
    snapshot_revision: str


def evaluate_closure_policy(
    snapshot: ClosureSnapshot, *, agent: AgentDefinitionBody, rules: list[RuleDefinitionRow]
) -> ClosureDecision:
    """Use the existing selector and expression engines, without mutable effects.

    Attestation kind/result strings have no built-in meaning. YAML supplies every
    requirement; this function supplies only snapshot queries and block semantics.
    """
    selected = select_closure_rules(agent, rules, (snapshot.event,))
    policy_data = {
        "agent": agent.model_dump(mode="json"),
        "rules": [
            {
                "id": row.id,
                "name": row.name,
                "priority": row.priority,
                "definition": row.definition_json,
            }
            for row in selected
        ],
    }
    policy_revision = _digest(policy_data)
    context = snapshot.model_dump(mode="json")
    all_records = context["attestations"]
    superseded = {record["supersedes_id"] for record in all_records if record["supersedes_id"]}
    active = sorted(
        (record for record in all_records if record["id"] not in superseded),
        key=lambda record: record["sequence"],
    )
    context["all_attestations"] = all_records
    context["attestations"] = active

    evaluator = SafeExpressionEvaluator(context, build_closure_condition_helpers(context))
    blockers = []
    for row in selected:
        try:
            body = RuleDefinitionBody.model_validate(row.definition_json)
            if any(effect.type != "block" for effect in body.resolved_effects):
                raise ValueError(f"{snapshot.event} rules may only return block decisions")
            if body.when is not None and not evaluator.evaluate(body.when):
                continue
            for effect in body.resolved_effects:
                if effect.when is None or evaluator.evaluate(effect.when):
                    blockers.append(effect.reason or f"Closure blocked by {row.name}")
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            blockers.append(f"Invalid closure rule {row.name}: {exc}")
    return ClosureDecision(
        not blockers,
        tuple(blockers),
        policy_revision,
        _digest({"snapshot": snapshot.model_dump(mode="json"), "policy": policy_revision}),
    )


def build_closure_condition_helpers(context: dict[str, Any]) -> dict[str, Callable[..., Any]]:
    """The closure engine's function contract; construction performs no queries."""

    def latest_attestation(kind: str) -> dict[str, Any]:
        return next(
            (
                record
                for record in reversed(context.get("attestations", []))
                if record["kind"] == kind
            ),
            {},
        )

    return {
        "latest_attestation": latest_attestation,
        "any": any,
        "all": all,
        "len": len,
        "str": str,
        "int": int,
        "bool": bool,
        "paths_match": paths_match,
        "holds_capability": holds_capability,
        "timestamp": lambda value: datetime.fromisoformat(value).timestamp(),
    }


def paths_match(paths: list[str], patterns: list[str]) -> bool:
    """Match changed repository paths against directory prefixes or path globs."""
    return any(
        path == pattern.rstrip("/")
        or path.startswith(pattern.rstrip("/") + "/")
        or PurePosixPath(path).full_match(pattern)
        for path in paths
        for pattern in patterns
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
