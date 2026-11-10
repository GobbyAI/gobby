"""Every bundled seat's per-turn context ships whole inside the additionalContext limit.

The per-turn context is the seat's prompt for each surface it supports, plus every
turn-start rule contributor the definition selects. It is rendered as a fresh epoch:
the definition's declared variables are in place and nothing is loaded yet, so every
declared instruction is still owed. Rule conditions are ignored, so the measure is the
worst delivery: everything that could ship together in one turn. Instructions a claimed
task adds are not modeled, so the totals are a floor.

Three measures run per seat surface. The synthetic-policy measure renders the prompt and
the bundled rules against a synthetic project policy, the reference criterion 1 names; it
must fit with nothing omitted. The bundled-owned measure renders the same bundled text
against the installed policy and must fit on its own. The installed-policy overflow is
measured, not gated: the operator-owned share may exceed the limit, and paging must still
drain whatever overflows, in order, with no stall.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import jinja2
import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    ContextPart,
    ContextPiece,
    deliver_context,
)
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.engine.templating import build_rule_condition_helpers
from gobby.workflows.selectors import rule_matches_agent
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

_PROJECT_RULES = Path(__file__).resolve().parents[2] / ".gobby" / "workflows" / "rules"
_SYNCED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_SURFACES: tuple[tuple[Literal["persona", "agent"], Literal["persona", "spawn"]], ...] = (
    ("persona", "persona"),
    ("agent", "spawn"),
)


def _seat_variables(agent: AgentDefinitionBody) -> dict[str, Any]:
    """The variables a seat starts with: the definition's pre-seeded and step-declared values."""
    variables = dict(agent.workflows.variables)
    if agent.step_workflow is not None:
        variables.update(agent.step_workflow.variables)
    return variables


def _environment(variables: dict[str, Any]) -> jinja2.Environment:
    """Render rule templates with the engine's own helper registry for this seat."""
    environment = jinja2.Environment(undefined=jinja2.Undefined)
    environment.globals.update(
        build_rule_condition_helpers({"variables": variables, "project": {}})
    )
    return environment


def _rule_rows() -> list[RuleDefinitionRow]:
    paths = [
        path
        for path in sorted(get_bundled_rules_path().rglob("*.yaml"))
        if "deprecated" not in path.parts
    ]
    if _PROJECT_RULES.is_dir():
        paths.extend(sorted(_PROJECT_RULES.glob("*.yaml")))
    rows: list[RuleDefinitionRow] = []
    for path in paths:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        file_tags = list(data.get("tags") or [])
        source: Literal["installed", "project"] = (
            "project" if path.parent == _PROJECT_RULES else "installed"
        )
        for name, rule in (data.get("rules") or {}).items():
            tags = list(rule.get("tags", file_tags))
            if source == "installed" and "gobby" not in tags:
                tags.append("gobby")
            rows.append(
                RuleDefinitionRow(
                    id=name,
                    name=name,
                    enabled=True,
                    enabled_pinned=False,
                    priority=int(rule.get("priority", 100)),
                    definition_json=dict(rule),
                    source=source,
                    tags=tags,
                    created_at=_SYNCED_AT,
                    updated_at=_SYNCED_AT,
                )
            )
    return rows


def _project_policy(rows: list[RuleDefinitionRow]) -> dict[str, Any]:
    for row in rows:
        if row.source != "project":
            continue
        for effect in row.definition_json.get("effects") or []:
            if effect.get("type") == "set_variable" and effect.get("variable") == "project_policy":
                return dict(effect.get("value") or {})
    return {}


def _prompt(
    agent: AgentDefinitionBody,
    surface: Literal["persona", "agent"],
    policy: dict[str, Any],
) -> str:
    variables = {**_seat_variables(agent), "project_policy": policy, "seat_context": {}}
    return agent.prompt_for(surface, variables=variables)


def _turn_start_parts(
    agent: AgentDefinitionBody,
    rows: list[RuleDefinitionRow],
    policy: dict[str, Any],
    owner: Literal["installed", "project"] | None = None,
) -> list[tuple[str, str]]:
    variables = _seat_variables(agent)
    environment = _environment(variables)
    parts: list[tuple[str, str]] = []
    for row in rows:
        definition = row.definition_json
        if definition.get("event") != "turn_start":
            continue
        if owner is not None and row.source != owner:
            continue
        if not rule_matches_agent(agent, row, definition_json=definition):
            continue
        for effect in definition.get("effects") or []:
            if effect.get("type") == "inject_context":
                template = environment.from_string(effect.get("template") or "")
                rendered = template.render(
                    project_policy=policy, seat_context={}, variables=variables
                )
                parts.append((f"rule:{row.name}", rendered))
    return parts


def _bundled_cases() -> Iterator[Any]:
    rows = _rule_rows()
    policy = _project_policy(rows)
    for path in sorted(get_bundled_agents_path().rglob("*.yaml")):
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        data.setdefault("name", path.stem)
        agent = AgentDefinitionBody.model_validate(data)
        for surface, definition_surface in _SURFACES:
            if agent.supports_surface(definition_surface):
                yield pytest.param(
                    agent,
                    surface,
                    rows,
                    policy,
                    id=f"{agent.name}-{surface}",
                )


_CASES = list(_bundled_cases())
_BUNDLED_ROWS = [row for row in _rule_rows() if row.source == "installed"]

# Criterion 1 reference: a synthetic stand-in for the operator-owned project policy. It carries
# the keys bundled rules read, with short values, so the measurement is bundled text alone.
_SYNTHETIC_POLICY: dict[str, Any] = {
    "integration_branch": "integration",
    "operator_channel": "operator",
    "quiet_hours": {"timezone": "UTC"},
    "role_guidance": {},
    "deferral_labels": [],
    "security_paths": [],
    "roots": [],
    "release_guidance": "",
    "workspace_layout": {},
    "agent_model_defaults": {},
    "default_model_denylist": [],
}


def test_bundled_definitions_and_the_project_policy_are_enumerated() -> None:
    assert _CASES, "no bundled definition surface was enumerated"
    assert _project_policy(_rule_rows()), "the checked-in project policy was not found"


@pytest.mark.parametrize(
    ("agent", "surface"),
    [pytest.param(case.values[0], case.values[1], id=case.id) for case in _CASES],
)
def test_bundled_definitions_fit_against_a_synthetic_project_policy(
    agent: AgentDefinitionBody,
    surface: Literal["persona", "agent"],
) -> None:
    """Criterion 1 reference: bundled prompt and rules, zero omission, no operator policy."""
    parts: list[ContextPart] = [
        ("agent_prompt", _prompt(agent, surface, _SYNTHETIC_POLICY)),
        *_turn_start_parts(agent, _BUNDLED_ROWS, _SYNTHETIC_POLICY),
    ]

    delivery = deliver_context(parts, limit=ADDITIONAL_CONTEXT_LIMIT)

    assert not delivery.remaining, (
        f"{agent.name} ({surface}) bundled context against the synthetic policy is "
        f"{_size(parts)} characters; waiting: {[piece.label for piece in delivery.remaining]}"
    )


@pytest.mark.parametrize(("agent", "surface", "rows", "policy"), _CASES)
def test_bundled_owned_context_fits_the_limit_on_its_own(
    agent: AgentDefinitionBody,
    surface: Literal["persona", "agent"],
    rows: list[RuleDefinitionRow],
    policy: dict[str, Any],
) -> None:
    """Guard the share this repo owns: the prompt plus every installed turn-start rule."""
    parts = [
        ("agent_prompt", _prompt(agent, surface, policy)),
        *_turn_start_parts(agent, rows, policy, owner="installed"),
    ]
    total = len("\n\n".join(text for _, text in parts if text))

    delivery = deliver_context(parts, limit=ADDITIONAL_CONTEXT_LIMIT)

    assert not delivery.remaining, (
        f"{agent.name} ({surface}) bundled-owned context is {total} characters, "
        f"{total - ADDITIONAL_CONTEXT_LIMIT} over the limit; "
        f"waiting: {[piece.label for piece in delivery.remaining]}"
    )


def _size(parts: Sequence[ContextPart | ContextPiece]) -> int:
    texts = [part.text if isinstance(part, ContextPiece) else part[1] for part in parts]
    return len("\n\n".join(text for text in texts if text))


def test_installed_policy_overflow_drains_in_consecutive_deliveries() -> None:
    """The operator-owned share is measured, not gated: whatever overflows must still drain."""
    for case in _CASES:
        agent, surface, rows, policy = case.values
        queue: list[ContextPart | ContextPiece] = [
            ("agent_prompt", _prompt(agent, surface, policy)),
            *_turn_start_parts(agent, rows, policy),
        ]
        while queue:
            delivery = deliver_context(queue, limit=ADDITIONAL_CONTEXT_LIMIT)
            assert len(delivery.text) <= ADDITIONAL_CONTEXT_LIMIT, f"{agent.name} ({surface})"
            assert _size(delivery.remaining) < _size(queue), f"{agent.name} ({surface}) stalled"
            queue = list(delivery.remaining)
