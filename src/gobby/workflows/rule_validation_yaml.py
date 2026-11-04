"""Grouped YAML adapter for rule preflight and installation."""

from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.utils.strict_yaml import load_strict_yaml
from gobby.workflows.definitions import split_rule_definition_data
from gobby.workflows.rule_validation import RuleValidation, RuleValidationIssue, validate_rule


def normalize_rule_yaml_entry(
    rule: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve grouped-file defaults and the single-effect authoring shorthand."""
    data = dict(rule)
    for key in ("group", "audience", "tags", "sources"):
        if defaults and key in defaults:
            data.setdefault(key, defaults[key])
    data.pop("override", None)
    sources = data.pop("sources", None)
    if sources is not None and (
        not isinstance(sources, list) or not all(isinstance(item, str) for item in sources)
    ):
        raise ValueError("sources must be a list of strings")
    if "effect" in data:
        if "effects" in data:
            raise ValueError("Declare either effect or effects")
        data["effects"] = [data.pop("effect")]
    body, metadata = split_rule_definition_data(data)
    return body, metadata


def validate_rule_yaml(
    text: str, *, source: str | None = None, context: Mapping[str, Any] | None = None
) -> list[RuleValidation]:
    """Preflight every entry, including disabled rules; preserve per-rule errors."""

    def invalid(name: str, message: str) -> RuleValidation:
        return RuleValidation(
            name=name,
            source=source,
            issues=[RuleValidationIssue("definition", "yaml", message)],
        )

    try:
        data = load_strict_yaml(text)
    except yaml.YAMLError as exc:
        return [invalid("<file>", str(exc))]
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict) or not data["rules"]:
        return [invalid("<file>", "Expected a non-empty rules mapping")]
    reports = []
    for name, rule in data["rules"].items():
        if not isinstance(name, str) or not isinstance(rule, dict):
            reports.append(invalid(str(name), "Expected a rule name and mapping"))
            continue
        try:
            body, _ = normalize_rule_yaml_entry(rule, defaults=data)
        except (ValueError, ValidationError) as exc:
            reports.append(invalid(name, str(exc)))
            continue
        reports.append(validate_rule(body, name=name, source=source, context=context))
    return reports
