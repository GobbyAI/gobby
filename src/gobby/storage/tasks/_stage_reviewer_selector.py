"""Reviewer-agent selector parsing and resolution for task stages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._models import VALID_CATEGORIES, VALID_TASK_TYPES


class ReviewerAgentSelectorError(ValueError):
    """Raised when a reviewer-agent selector payload is malformed."""


@dataclass(frozen=True, slots=True)
class ReviewerAgentSelectorRule:
    category: str | None
    task_type: str | None
    reviewer_agent: str


@dataclass(frozen=True, slots=True)
class ReviewerAgentSelector:
    rules: tuple[ReviewerAgentSelectorRule, ...]
    default: str | None


def normalize_reviewer_agent_selector(raw_selector: Any, *, stage_name: str) -> str | None:
    """Validate selector YAML/JSON shape and return canonical JSON."""
    selector = parse_reviewer_agent_selector(raw_selector, stage_name=stage_name)
    if selector is None:
        return None
    payload: dict[str, Any] = {"rules": [_rule_payload(rule) for rule in selector.rules]}
    if selector.default is not None:
        payload["default"] = selector.default
    return json.dumps(payload, sort_keys=True)


def parse_reviewer_agent_selector_json(
    selector_json: str | None,
    *,
    stage_name: str,
) -> ReviewerAgentSelector | None:
    if not selector_json:
        return None
    try:
        raw_selector = json.loads(selector_json)
    except json.JSONDecodeError as exc:
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector_json must be valid JSON"
        ) from exc
    return parse_reviewer_agent_selector(raw_selector, stage_name=stage_name)


def parse_reviewer_agent_selector(
    raw_selector: Any,
    *,
    stage_name: str,
) -> ReviewerAgentSelector | None:
    if raw_selector is None:
        return None
    if not isinstance(raw_selector, Mapping):
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector must be a mapping"
        )
    unknown_keys = set(raw_selector) - {"rules", "default"}
    if unknown_keys:
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector has unknown fields: {sorted(unknown_keys)}"
        )

    raw_rules = raw_selector.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector.rules must be a list"
        )
    rules = tuple(_parse_rule(stage_name, index, rule) for index, rule in enumerate(raw_rules))
    default = _optional_non_empty_string(
        raw_selector.get("default"),
        f"Stage {stage_name} reviewer_agent_selector.default",
    )
    if not rules and default is None:
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector must define rules or default"
        )
    return ReviewerAgentSelector(rules=rules, default=default)


def validate_reviewer_agent_selector_json(selector_json: str | None, *, stage_name: str) -> None:
    parse_reviewer_agent_selector_json(selector_json, stage_name=stage_name)


def resolve_stage_reviewer(
    db: DatabaseProtocol,
    task_id: str,
    registry_entry: object,
    *,
    stage_reviewer_agent: str | None = None,
) -> str | None:
    """Resolve the reviewer to snapshot for a task stage."""
    if stage_reviewer_agent:
        return stage_reviewer_agent
    fixed_reviewer = _field(registry_entry, "reviewer_agent")
    if fixed_reviewer:
        return str(fixed_reviewer)

    selector = parse_reviewer_agent_selector_json(
        _field(registry_entry, "reviewer_agent_selector_json"),
        stage_name=str(_field(registry_entry, "name", "stage")),
    )
    if selector is None:
        return None

    row = db.fetchone("SELECT category, task_type FROM tasks WHERE id = ?", (task_id,))
    category = row["category"] if row is not None else None
    task_type = row["task_type"] if row is not None else None

    for rule in selector.rules:
        if rule.category is not None and rule.category == category:
            return rule.reviewer_agent
    for rule in selector.rules:
        if rule.task_type is not None and rule.task_type == task_type:
            return rule.reviewer_agent
    return selector.default


def _parse_rule(stage_name: str, index: int, raw_rule: Any) -> ReviewerAgentSelectorRule:
    if not isinstance(raw_rule, Mapping):
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector.rules[{index}] must be a mapping"
        )
    unknown_keys = set(raw_rule) - {"category", "task_type", "reviewer_agent"}
    if unknown_keys:
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector.rules[{index}] has unknown fields: "
            f"{sorted(unknown_keys)}"
        )
    has_category = "category" in raw_rule
    has_task_type = "task_type" in raw_rule
    if has_category == has_task_type:
        raise ReviewerAgentSelectorError(
            f"Stage {stage_name} reviewer_agent_selector.rules[{index}] must set exactly "
            "one of category or task_type"
        )

    reviewer_agent = _required_non_empty_string(
        raw_rule.get("reviewer_agent"),
        f"Stage {stage_name} reviewer_agent_selector.rules[{index}].reviewer_agent",
    )
    category = None
    task_type = None
    if has_category:
        category = _required_non_empty_string(
            raw_rule.get("category"),
            f"Stage {stage_name} reviewer_agent_selector.rules[{index}].category",
        ).lower()
        if category not in VALID_CATEGORIES:
            raise ReviewerAgentSelectorError(
                f"Stage {stage_name} reviewer_agent_selector.rules[{index}] "
                f"has invalid category: {category}"
            )
    if has_task_type:
        task_type = _required_non_empty_string(
            raw_rule.get("task_type"),
            f"Stage {stage_name} reviewer_agent_selector.rules[{index}].task_type",
        ).lower()
        if task_type not in VALID_TASK_TYPES:
            raise ReviewerAgentSelectorError(
                f"Stage {stage_name} reviewer_agent_selector.rules[{index}] "
                f"has invalid task_type: {task_type}"
            )
    return ReviewerAgentSelectorRule(
        category=category,
        task_type=task_type,
        reviewer_agent=reviewer_agent,
    )


def _rule_payload(rule: ReviewerAgentSelectorRule) -> dict[str, str]:
    payload = {"reviewer_agent": rule.reviewer_agent}
    if rule.category is not None:
        payload["category"] = rule.category
    if rule.task_type is not None:
        payload["task_type"] = rule.task_type
    return payload


def _required_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewerAgentSelectorError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_non_empty_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _required_non_empty_string(value, label)


def _field(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)
