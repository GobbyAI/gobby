"""Loader and public API for the rule-pass corpus.

A case is a synthetic rule body set, an event, optional seam failures and an ``expect``
block. The ``expect`` block is recorded from the Python engine by ``rules_recorder``; it
is never written by hand. Coverage checks live in ``rules_coverage`` and the normalizer
vectors in ``rules_vectors``. This module keeps the loader and the names the tests import.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeGuard, get_args

import yaml

from gobby.workflows.definitions import (
    RuleDefinitionBody,
    RuleEffect,
)
from gobby.workflows.rule_validation_yaml import normalize_rule_yaml_entry
from tests.contracts import rules_seed

CORPUS_DIR = Path(__file__).resolve().parent / "rules"
BUNDLED_RULES_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "workflows"
    / "rules"
)
SCHEMA_VERSION = 1
SYNTHETIC_RULE_PREFIX = "synthetic-"
EXPECT_KEYS = (
    "outcome_class",
    "eager_variable_delta",
    "on_receipt_payload",
    "effect_log",
    "audit_records",
)
SKIPPED_GROUPS_KEY = "skipped_groups"
RESPONSE_KEY = "response"
EFFECT_TYPES: tuple[str, ...] = tuple(get_args(RuleEffect.model_fields["type"].annotation))
DELIVERY_CLASSES: tuple[str, ...] = tuple(get_args(RuleEffect.model_fields["delivery"].annotation))
# The file-level keys the engine's file loader reads (workflows/sync_rules.py) besides rules.
BUNDLED_TOP_LEVEL_KEYS = frozenset(
    {"rules", "version", "tags", "description", "group", "audience", "sources"}
)
CALLER_KIND = "seam_caller"
INLINE_CALLER = "inline_run_command"
# The one fake RTK a case may opt into (``proxy``); its script lives in rules_engine_seams.
RTK_FAKE = "echo-prefix"
_LABELLED_ID = re.compile(r"^S-(FAM|OBS)-(.+)-(?:ALLOW|BLOCK|SEAMERR|OBSERVED|NOTOBSERVED)$")


def _label_for(scenario_id: str) -> tuple[str, str] | None:
    """Return ``(axis, label)`` a scenario ID names, or None for an unlabelled ID."""
    match = _LABELLED_ID.match(scenario_id)
    if match is None:
        return None
    axis = "family" if match.group(1) == "FAM" else "observer"
    return axis, match.group(2).lower().replace("-", "_")


def _label_gaps(case: dict[str, Any]) -> list[str]:
    gaps: set[str] = set()
    for scenario_id in case.get("scenario_ids", []):
        labelled = _label_for(scenario_id)
        if labelled is None:
            continue
        axis, label = labelled
        if case.get(axis) != label:
            gaps.add(axis)
    return sorted(gaps)


def validate_case(case: dict[str, Any]) -> None:
    """Raise ``ValueError`` naming any missing ``expect`` section or malformed field."""
    case_id = str(case.get("id", "<no id>"))
    if case.get("provenance") != "synthetic":
        raise ValueError(
            f"{case_id}: provenance must be 'synthetic', got {case.get('provenance')!r}"
        )
    for key in ("scenario_ids", "rules", "event", "seam_failures", "expect"):
        if key not in case:
            raise ValueError(f"{case_id}: case has no {key!r} section")
    expect = case["expect"]
    for key in EXPECT_KEYS:
        if key not in expect:
            raise ValueError(f"{case_id}: expect is missing the {key!r} section")
    if not isinstance(case["rules"], list):
        raise ValueError(f"{case_id}: rules must be a list")
    for rule in case["rules"]:
        name = str(rule.get("name", ""))
        if not name.startswith(SYNTHETIC_RULE_PREFIX):
            raise ValueError(
                f"{case_id}: rule name {name!r} must start with {SYNTHETIC_RULE_PREFIX!r}"
            )
        for effect in rule.get("effects", []):
            if effect.get("type") not in EFFECT_TYPES:
                raise ValueError(f"{case_id}: unknown effect type {effect.get('type')!r}")
    gaps = _label_gaps(case)
    if gaps:
        raise ValueError(f"{case_id}: scenario IDs need a matching {', '.join(gaps)} label")
    _validate_engine_path_keys(case_id, case)


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    """True for a JSON number: an int or float, never a bool, and never NaN or infinite."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _validate_engine_path_keys(case_id: str, case: dict[str, Any]) -> None:
    """Refuse a malformed seed, POSIX marker, proxy opt-in, skip-group label or inline input."""
    rules_seed.validate_seed(case_id, case.get("seed"))
    if "posix_only" in case and not isinstance(case["posix_only"], bool):
        raise ValueError(f"{case_id}: posix_only must be a bool, got {case['posix_only']!r}")
    if "proxy" in case and case["proxy"] != {"rtk": RTK_FAKE}:
        raise ValueError(f"{case_id}: proxy must be {{'rtk': {RTK_FAKE!r}}}, got {case['proxy']!r}")
    if "engine_skip_group" in case:
        label = case["engine_skip_group"]
        if not isinstance(label, str) or not label:
            raise ValueError(f"{case_id}: engine_skip_group must be a non-empty string")
    if "pairs_with" in case and not isinstance(case["pairs_with"], str):
        raise ValueError(f"{case_id}: pairs_with must name a case id")
    spec = case.get("input")
    spec = spec if isinstance(spec, dict) else {}
    inline = case.get("kind") == CALLER_KIND and case.get("caller") == INLINE_CALLER
    if inline:
        seed = case.get("seed")
        if not isinstance(seed, dict) or seed.get("session") is not True:
            raise ValueError(f"{case_id}: an {INLINE_CALLER} case needs seed session true")
    if "command_boundary" in spec:
        if not inline:
            raise ValueError(f"{case_id}: command_boundary is for {INLINE_CALLER} cases only")
        boundary = spec["command_boundary"]
        duration = boundary.get("duration") if isinstance(boundary, dict) else None
        context = boundary.get("context") if isinstance(boundary, dict) else None
        if (
            not isinstance(boundary, dict)
            or set(boundary) != {"duration", "context"}
            or not _is_finite_number(duration)
            or duration < 0
            or not isinstance(context, str)
            or not context
        ):
            raise ValueError(
                f"{case_id}: command_boundary must be {{'duration': finite number >= 0, "
                f"'context': non-empty str}}, got {boundary!r}"
            )
        if "deadline" in spec:
            raise ValueError(f"{case_id}: give deadline_remaining or deadline, not both")
        remaining = spec.get("deadline_remaining")
        if remaining is not None and not _is_finite_number(remaining):
            raise ValueError(
                f"{case_id}: deadline_remaining must be a finite number or null, got {remaining!r}"
            )
    elif "deadline_remaining" in spec:
        raise ValueError(f"{case_id}: deadline_remaining needs an input.command_boundary")


def load_corpus(corpus_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(manifest, cases)`` for a corpus directory.

    Validates ``schema_version``, case kinds and seam names against the manifest, and checks
    every registry seam against the real attribute it wraps. Raises ``ValueError`` naming
    the offender.
    """
    manifest_path = corpus_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{manifest_path.name}: schema_version {version!r} is not {SCHEMA_VERSION}"
        )
    registry = seam_registry()
    for kind, (owner, attribute) in registry.items():
        if not hasattr(owner, attribute):
            owner_name = getattr(owner, "__name__", type(owner).__name__)
            raise ValueError(f"seam {kind!r}: {owner_name} has no attribute {attribute!r} to wrap")
    seams = set(manifest.get("seams", []))
    unknown_seams = sorted(seams - set(registry))
    if unknown_seams:
        raise ValueError(f"{manifest_path.name}: seam(s) not in the seam registry: {unknown_seams}")
    kinds = set(manifest.get("case_kinds", []))
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted((corpus_dir / "cases").glob("*.json")):
        document = json.loads(path.read_text())
        for case in document["cases"]:
            validate_case(case)
            if case.get("kind") not in kinds:
                raise ValueError(f"{case['id']}: unknown case kind {case.get('kind')!r}")
            for seam in case.get("seam_failures", []):
                if seam not in seams:
                    raise ValueError(f"{case['id']}: unknown seam {seam!r}")
            if case["id"] in seen:
                raise ValueError(f"duplicate case id {case['id']!r}")
            seen.add(case["id"])
            cases.append(case)
    for case in cases:
        partner = case.get("pairs_with")
        if partner is not None and partner not in seen:
            raise ValueError(f"{case['id']}: pairs_with names no loaded case: {partner!r}")
    return manifest, cases


def load_rule_body(path: Path) -> dict[str, Any]:
    """Parse one rule YAML file and raise ``ValueError`` naming any unknown field.

    Each rule is split into row metadata and body, and the body is validated by the
    engine's own models, so an unknown effect field fails here as it does at sync.
    """
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: the file is not a mapping")
    unknown = sorted(set(data) - BUNDLED_TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"{path.name}: unknown top-level field(s) {unknown}")
    rules = data.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise ValueError(f"{path.name}: no rules mapping")
    defaults = {key: data[key] for key in ("group", "audience", "tags", "sources") if key in data}
    parsed: dict[str, Any] = {}
    for name, rule in rules.items():
        try:
            # The engine's own entry normalizer resolves the single-effect shorthand and the
            # grouped-file defaults before the body is split and validated.
            body, metadata = normalize_rule_yaml_entry(
                {"name": name, **(rule or {})}, defaults=defaults
            )
            RuleDefinitionBody.model_validate(body)
        except ValueError as exc:
            raise ValueError(f"{path.name}: rule {name!r}: {exc}") from exc
        parsed[name] = {"body": body, "metadata": metadata}
    return {"file": path.name, "rules": parsed}


def load_bundled_rules() -> list[dict[str, Any]]:
    """Return one parsed body per bundled rule YAML file, loaded through the engine loader."""
    return [load_rule_body(path) for path in sorted(BUNDLED_RULES_DIR.rglob("*.yaml"))]


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming the first required item on ``axis`` that no case carries.

    A response field counts as set when the case's ``expect.response`` carries the key with
    a value that differs from that field's ``HookResponse`` default. A missing key is not set.
    """
    from tests.contracts import rules_coverage

    rules_coverage.check_coverage(corpus_dir, axis)


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python rule engine and return the observed ``expect`` block."""
    from tests.contracts import rules_recorder

    return rules_recorder.replay_case(case)


def record_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one case with every seam wrapped and return the ordered seam call log."""
    from tests.contracts import rules_recorder

    return rules_recorder.record_case(case)


def record_corpus(corpus_dir: Path) -> None:
    """Fill each case's ``expect`` block with the recorded engine output.

    Runs only when the ``GOBBY_CORPUS_RECORD`` environment variable is 1; otherwise the
    corpus files are left unchanged, so a default run replays. Each case is written back
    into the case file it was read from, with the same document layout.
    """
    from tests.contracts import rules_recorder

    rules_recorder.record_corpus(corpus_dir)


def replay_normalizer_vector(vector: dict[str, Any]) -> Any:
    """Call the normalizer entry point named by a vector and return its observed output."""
    from tests.contracts import rules_vectors

    return rules_vectors.replay_vector(vector)


def seam_registry() -> dict[str, tuple[object, str]]:
    """Map each seam kind to the owner (class or module) and attribute the recorder wraps."""
    from tests.contracts import rules_recorder

    return rules_recorder.seam_registry()


def find_bundled_name_mentions(paths: Iterable[Path], names: Iterable[str]) -> list[str]:
    """Return one finding per file that names a bundled definition as a whole token."""
    patterns = [
        (name, re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])"))
        for name in names
    ]
    findings: list[str] = []
    for path in paths:
        text = Path(path).read_text()
        for name, pattern in patterns:
            if pattern.search(text):
                findings.append(f"{Path(path).name} names {name!r}")
    return findings
