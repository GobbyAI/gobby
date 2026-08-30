"""Classify and persist rule-effect delivery dispositions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gobby.storage.definitions._shared import (
    apply_definition_update,
    encode_json_value,
    touch_revision,
)
from gobby.storage.definitions.rules import RuleDefinitionManager, _lock_live_row
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

_PAYLOAD_TYPES = frozenset({"block", "inject_context", "load_skill", "set_display_content"})

_Delivery = Literal["eager", "on_receipt"]


@dataclass(frozen=True)
class DispositionOutcome:
    definition: dict[str, Any]
    changed: bool
    errors: tuple[str, ...]


class RuleDispositionMigrationError(RuntimeError):
    def __init__(self, diagnostics: list[str]) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "; ".join(diagnostics) if diagnostics else "rule delivery disposition failed"
        )


class DispositionAmbiguousError(ValueError):
    def __init__(self, diagnostic: str) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic)


def apply_rule_delivery_dispositions(
    rule_name: str,
    definition: Mapping[str, Any],
) -> DispositionOutcome:
    """Return *definition* with explicit ``on_receipt`` grouping, or diagnostics."""
    rewritten = dict(definition)
    key, effects = _effect_entries(definition)
    if not effects:
        return DispositionOutcome(definition=rewritten, changed=False, errors=())

    rule_when = definition.get("when") if isinstance(definition.get("when"), str) else None
    decisions: list[tuple[dict[str, Any], _Delivery | None, str | None]] = []
    errors: list[str] = []
    for index, effect in enumerate(effects):
        if not isinstance(effect, dict):
            decisions.append((effect, None, None))
            continue
        want, error = _classify_effect(
            rule_name,
            index,
            effect,
            effects,
            rule_when,
        )
        if error is not None:
            errors.append(error)
        decisions.append((effect, want, error))

    if errors:
        return DispositionOutcome(
            definition=dict(definition),
            changed=False,
            errors=tuple(errors),
        )

    changed = False
    new_effects: list[Any] = []
    for effect, desired, _error in decisions:
        if not isinstance(effect, dict) or desired is None:
            new_effects.append(effect)
            continue
        copy = dict(effect)
        if desired == "on_receipt" and copy.get("delivery", "eager") != "on_receipt":
            copy["delivery"] = "on_receipt"
            changed = True
        new_effects.append(copy)

    if key == "effect":
        rewritten["effect"] = new_effects[0] if new_effects else rewritten.get("effect")
    else:
        rewritten["effects"] = new_effects
    return DispositionOutcome(definition=rewritten, changed=changed, errors=())


def prepare_rule_definition_for_persist(
    rule_name: str,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    """Rewrite a definition for create/update/import, or raise on ambiguity."""
    outcome = apply_rule_delivery_dispositions(rule_name, definition)
    if outcome.errors:
        raise DispositionAmbiguousError(outcome.errors[0])
    return outcome.definition


def migrate_rule_delivery_dispositions(db: HubDatabase) -> dict[str, Any]:
    """Write explicit ``on_receipt`` onto live rows that match the one-shot criterion."""
    manager = RuleDefinitionManager(db)
    rows = manager.list_all()
    errors: list[str] = []
    skipped = 0
    plans: list[Any] = []
    for row in rows:
        definition = row.definition_json
        if not isinstance(definition, dict):
            skipped += 1
            continue
        outcome = apply_rule_delivery_dispositions(row.name, definition)
        if outcome.errors:
            errors.extend(outcome.errors)
            continue
        if not outcome.changed:
            skipped += 1
            continue
        plans.append((row, outcome.definition))

    if errors:
        return {"success": False, "updated": 0, "skipped": skipped, "errors": errors}
    if not plans:
        return {"success": True, "updated": 0, "skipped": skipped, "errors": []}

    try:
        with db.transaction() as txn:
            updated = 0
            for row, replacement in plans:
                locked = _lock_live_row(txn, row.id)
                if locked.updated_at != row.updated_at:
                    raise RuleDispositionMigrationError(
                        [f"delivery disposition: Rule '{row.name}' concurrent edit"]
                    )
                apply_definition_update(
                    txn,
                    "rule_definitions",
                    row.id,
                    {
                        "definition_json": encode_json_value(replacement),
                        "updated_at": utc_now(),
                    },
                    what="Rule definition",
                )
                updated += 1
            if updated:
                touch_revision(txn, "rules")
    except RuleDispositionMigrationError as exc:
        return {
            "success": False,
            "updated": 0,
            "skipped": skipped,
            "errors": list(exc.diagnostics),
        }
    except Exception as exc:
        return {
            "success": False,
            "updated": 0,
            "skipped": skipped,
            "errors": [f"delivery disposition: partial failure: {exc}"],
        }

    return {"success": True, "updated": len(plans), "skipped": skipped, "errors": []}


def _effect_entries(definition: Mapping[str, Any]) -> tuple[str, list[Any]]:
    effects = definition.get("effects")
    if isinstance(effects, list):
        return "effects", list(effects)
    effect = definition.get("effect")
    if isinstance(effect, dict):
        return "effect", [effect]
    return "effects", []


def _is_one_shot_guard(variable: str, when: str | None) -> bool:
    if not when:
        return False
    escaped = re.escape(variable)
    if re.search(rf"not\s+variables\.get\(\s*['\"]{escaped}['\"]", when):
        return True
    if re.search(rf"variables\.get\(\s*['\"]{escaped}['\"][^)]*\)\s*!=", when):
        return True
    if re.search(rf"!=\s*variables\.get\(\s*['\"]{escaped}['\"]", when):
        return True
    return False


def _is_payload(effect: Mapping[str, Any]) -> bool:
    effect_type = effect.get("type")
    if effect_type in _PAYLOAD_TYPES:
        return True
    return effect_type == "mcp_call" and bool(effect.get("inject_result"))


def _suppressor_variable(effect: Mapping[str, Any]) -> str | None:
    effect_type = effect.get("type")
    if effect_type == "block":
        variable = effect.get("acknowledge_variable")
        return variable if isinstance(variable, str) and variable else None
    if effect_type == "mcp_call":
        variable = effect.get("success_variable")
        return variable if isinstance(variable, str) and variable else None
    return None


def _payload_effects(effects: Sequence[Any]) -> list[dict[str, Any]]:
    return [effect for effect in effects if isinstance(effect, dict) and _is_payload(effect)]


def _has_grouping_suppressor(
    effects: Sequence[Any],
    rule_when: str | None,
) -> bool:
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        if _suppressor_variable(effect):
            return True
        if (
            effect.get("type") == "set_variable"
            and isinstance(effect.get("variable"), str)
            and _is_one_shot_guard(effect["variable"], rule_when)
            and not effect.get("when")
        ):
            return True
    return False


def _classify_effect(
    rule_name: str,
    index: int,
    effect: Mapping[str, Any],
    effects: Sequence[Any],
    rule_when: str | None,
) -> tuple[_Delivery, str | None]:
    if _suppressor_variable(effect):
        return "on_receipt", None

    if effect.get("type") == "set_variable":
        variable = effect.get("variable")
        payloads = _payload_effects(effects)
        if isinstance(variable, str) and _is_one_shot_guard(variable, rule_when) and payloads:
            has_own_when = bool(effect.get("when"))
            payloads_with_when = [item for item in payloads if item.get("when")]
            payloads_without_when = [item for item in payloads if not item.get("when")]
            if has_own_when and payloads_without_when:
                return "eager", (
                    f"delivery disposition: Rule '{rule_name}' effect {index} "
                    f"(set_variable '{variable}'): ambiguous delivery suppressor"
                )
            if has_own_when and payloads_with_when:
                return "eager", None
            if not has_own_when:
                return "on_receipt", None
        return "eager", None

    if _is_payload(effect):
        if _has_grouping_suppressor(effects, rule_when):
            return "on_receipt", None
        return "eager", None

    return "eager", None
