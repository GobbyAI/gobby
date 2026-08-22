"""Manifest emitter for the yolo cap-exhausted fallback path (§2.21a).

Idempotent stub-manifest writer. File and parse failures append a
``## Yolo Fallbacks`` audit section and return ``"fallback_force_approve"``
so dispatch can advance lifecycle deterministically.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from gobby.plans.parser import (
    KIND_LINE_RE,
    Kind,
    PlanDocument,
    PlanKind,
    PlanParseError,
    PlanSection,
    compute_fence_mask,
    extract_section_dependencies,
    parse_plan,
    resolve_plan_id,
)
from gobby.tasks.categories import (
    AGENT_BY_IMPLEMENTATION_DOMAIN,
    DEVELOPMENT_FORWARD_LEAF_CATEGORIES,
    TDD_ELIGIBLE_CATEGORIES,
)

logger = logging.getLogger(__name__)

EmitOutcome = Literal[
    "fresh",
    "replaced_malformed",
    "noop_existing_valid",
    "fallback_force_approve",
]

__all__ = ["EmitOutcome", "derive_manifest_entries", "emit_stub_manifest"]

_HEADING_LINE_RE = re.compile(r"^(?P<marks>#{2,6})\s+")
_CATEGORY_RE = re.compile(r"\[category:\s*(?P<value>[a-z_]+)\]")
_TITLE_BRACKET_RE = re.compile(r"\s*(?:\[category:[^\]]+\]|\(depends:[^)]+\))")
_PHASE_REF_RE = re.compile(r"^P\d+$")
_FRONTEND_SIGNAL_RE = re.compile(
    r"\b(?:accessibility|browser|client|component|css|eslint|frontend|lighthouse|"
    r"next\.?js|playwright|react|routing|storybook|svelte|ui|vite|vue|webpack)\b",
    flags=re.IGNORECASE,
)
_BACKEND_SIGNAL_RE = re.compile(
    r"\b(?:api|backend|cache|database|daemon|endpoint|migration|model|postgres|"
    r"queue|schema|server|service|storage|worker)\b",
    flags=re.IGNORECASE,
)
_DEFAULT_CATEGORY = "code"
_AGENT_BY_CATEGORY: dict[str, str] = {
    "code": "backend-developer",
    "config": "backend-developer",
    "docs": "tech-writer",
    "refactor": "backend-developer",
    "test": "backend-developer",
}
_DEFAULT_AGENT_FALLBACK = "backend-developer"
_DEFAULT_TASK_TYPE = "feature"
_ROUTING_FIELDS = frozenset(
    {
        "assigned_agent",
        "category",
        "depends_on",
        "implementation_domain",
        "task_type",
        "tdd",
    }
)


class ManifestSynthesisError(ValueError):
    """Raised when deterministic manifest synthesis cannot produce valid dependencies."""


def derive_manifest_entries(
    document: PlanDocument,
    routing_decisions: Mapping[str, object],
) -> list[dict[str, object]]:
    """Derive canonical manifest entries from plan-owned facts and reviewed routing."""
    plan_id = resolve_plan_id(document.plan_id)
    deliverables = [section for section in document.sections if section.kind is Kind.deliverable]
    deliverable_ids = {section.section_id for section in deliverables}
    unknown_sections = sorted(set(routing_decisions) - deliverable_ids)
    if unknown_sections:
        raise ManifestSynthesisError(
            "routing decisions reference unknown deliverables: " + ", ".join(unknown_sections)
        )
    dependencies_by_section = _synthesized_dependencies(document, deliverables)
    section_by_id = {section.section_id: section for section in document.sections}
    deliverable_ids = {section.section_id for section in deliverables}
    by_phase = deliverables_by_phase(document, deliverables)
    entries: list[dict[str, object]] = []
    for section in deliverables:
        raw_decision = routing_decisions.get(section.section_id, {})
        if not isinstance(raw_decision, Mapping):
            raise ManifestSynthesisError(
                f"routing decision for {section.section_id!r} must be an object"
            )
        decision = dict(raw_decision)
        unknown_fields = sorted(set(decision) - _ROUTING_FIELDS)
        if unknown_fields:
            raise ManifestSynthesisError(
                f"routing decision for {section.section_id!r} has unsupported fields: "
                + ", ".join(unknown_fields)
            )
        entry = _synthesize_entry(
            plan_id,
            section,
            dependencies_by_section[section.section_id],
        )
        category = decision.get("category", entry["category"])
        if not isinstance(category, str) or not category:
            raise ManifestSynthesisError(
                f"routing decision for {section.section_id!r} has invalid category"
            )
        entry["category"] = category
        if "task_type" in decision:
            task_type = decision["task_type"]
            if not isinstance(task_type, str) or not task_type:
                raise ManifestSynthesisError(
                    f"routing decision for {section.section_id!r} has invalid task_type"
                )
            entry["task_type"] = task_type
        if "depends_on" in decision:
            entry["depends_on"] = list(
                _resolve_dependency_refs(
                    decision["depends_on"],
                    section_id=section.section_id,
                    section_by_id=section_by_id,
                    deliverable_ids=deliverable_ids,
                    deliverables_by_phase=by_phase,
                    require_list=True,
                )
            )
        if "tdd" in decision and not isinstance(decision["tdd"], bool):
            raise ManifestSynthesisError(
                f"routing decision for {section.section_id!r} has invalid tdd"
            )
        entry["validation_criteria"] = "\n".join(
            f"{item.item_id}: {item.prose}" for item in section.acceptance_items
        )
        entry["labels"] = [
            f"covers:{plan_id}:{section.section_id}:{item.item_id}"
            for item in section.acceptance_items
        ]
        entry.pop("assigned_agent", None)
        entry.pop("implementation_domain", None)
        if category == "code":
            if "assigned_agent" in decision:
                raise ManifestSynthesisError(
                    f"routing decision for {section.section_id!r} must use "
                    "implementation_domain for category code"
                )
            domain = decision.get("implementation_domain")
            if domain is None:
                domain = _implementation_domain_for(
                    section,
                    str(entry["title"]),
                    str(entry["validation_criteria"]),
                )
            elif not isinstance(domain, str) or not domain:
                raise ManifestSynthesisError(
                    f"routing decision for {section.section_id!r} has invalid implementation_domain"
                )
            entry["implementation_domain"] = domain
        else:
            if "implementation_domain" in decision:
                raise ManifestSynthesisError(
                    f"routing decision for {section.section_id!r} must use "
                    f"assigned_agent for category {category}"
                )
            agent = decision.get("assigned_agent")
            if agent is None:
                agent = _agent_for(
                    category,
                    section,
                    str(entry["title"]),
                    str(entry["validation_criteria"]),
                )
            elif not isinstance(agent, str) or not agent:
                raise ManifestSynthesisError(
                    f"routing decision for {section.section_id!r} has invalid assigned_agent"
                )
            entry["assigned_agent"] = agent
        entry["tdd"] = decision.get("tdd", category in TDD_ELIGIBLE_CATEGORIES)
        entries.append(entry)
    return entries


def emit_stub_manifest(
    plan_path: str | Path,
    *,
    by_actor: str = "dispatcher",
    plan_kind: PlanKind = PlanKind.implementation,
    plan_id: str | None = None,
) -> EmitOutcome:
    """Idempotent manifest emitter — see §2.21a.

    Sequence:
    1. Parse in ``parse_mode="draft"``.
    2. Valid manifest already present → ``"noop_existing_valid"``.
    3. Malformed manifest present → replace with synthesized one →
       ``"replaced_malformed"``.
    4. No manifest → synthesize one → ``"fresh"``.
    5. File or parse failures → append a ``## Yolo Fallbacks`` audit section →
       ``"fallback_force_approve"``.
    """
    if plan_kind is PlanKind.strategy:
        raise ManifestSynthesisError("strategy plans do not support manifest emission")

    path = Path(plan_path)
    try:
        return _emit(path, by_actor=by_actor, plan_kind=plan_kind, plan_id=plan_id)
    except (OSError, PlanParseError) as exc:
        logger.warning("Manifest emitter falling back for %s: %s", path, exc)
        _append_yolo_fallback(path, by_actor=by_actor, reason=f"emitter exception: {exc!r}")
        return "fallback_force_approve"


def _emit(path: Path, *, by_actor: str, plan_kind: PlanKind, plan_id: str | None) -> EmitOutcome:
    raw = path.read_text(encoding="utf-8")

    draft_doc: PlanDocument | None = None
    draft_error: PlanParseError | None = None
    try:
        draft_doc = parse_plan(
            path,
            plan_kind=plan_kind,
            parse_mode="draft",
            plan_id_override=plan_id,
        )
    except PlanParseError as exc:
        draft_error = exc

    if draft_doc is not None:
        if draft_doc.manifest_entries:
            try:
                parse_plan(
                    path,
                    plan_kind=plan_kind,
                    parse_mode="draft",
                    plan_id_override=plan_id,
                )
                return "noop_existing_valid"
            except PlanParseError:
                return _replace_existing_manifest(
                    path,
                    raw,
                    plan_kind=plan_kind,
                    by_actor=by_actor,
                    plan_id=plan_id,
                )
        return _emit_fresh(
            path,
            raw,
            draft_doc,
            by_actor=by_actor,
            plan_kind=plan_kind,
            plan_id=plan_id,
        )

    if _has_manifest_section(raw):
        return _replace_existing_manifest(
            path,
            raw,
            plan_kind=plan_kind,
            by_actor=by_actor,
            plan_id=plan_id,
        )

    if draft_error is None:
        raise RuntimeError(f"Plan parser returned neither document nor error for {path}")
    _append_yolo_fallback(
        path,
        by_actor=by_actor,
        reason=f"plan parse error in draft mode: {draft_error}",
    )
    return "fallback_force_approve"


def _replace_existing_manifest(
    path: Path,
    raw: str,
    *,
    plan_kind: PlanKind,
    by_actor: str,
    plan_id: str | None,
) -> EmitOutcome:
    stripped = _strip_manifest_section(raw)
    path.write_text(stripped, encoding="utf-8")

    try:
        stripped_doc = parse_plan(
            path,
            plan_kind=plan_kind,
            parse_mode="draft",
            plan_id_override=plan_id,
        )
    except PlanParseError as exc:
        path.write_text(raw, encoding="utf-8")
        _append_yolo_fallback(
            path,
            by_actor=by_actor,
            reason=f"plan still malformed after stripping manifest: {exc}",
        )
        return "fallback_force_approve"

    outcome = _emit_fresh(
        path,
        stripped,
        stripped_doc,
        by_actor=by_actor,
        plan_kind=plan_kind,
        plan_id=plan_id,
    )
    if outcome == "fresh":
        return "replaced_malformed"
    return outcome


def _emit_fresh(
    path: Path,
    body: str,
    document: PlanDocument,
    *,
    by_actor: str,
    plan_kind: PlanKind,
    plan_id: str | None,
) -> EmitOutcome:
    deliverables = [section for section in document.sections if section.kind is Kind.deliverable]
    if not deliverables:
        _append_yolo_fallback(
            path,
            by_actor=by_actor,
            reason="plan has no kind: deliverable sections to synthesize a manifest from",
        )
        return "fallback_force_approve"

    try:
        entries = derive_manifest_entries(document, {})
    except ManifestSynthesisError as exc:
        _append_yolo_fallback(path, by_actor=by_actor, reason=str(exc))
        return "fallback_force_approve"
    new_text = _write_manifest_section(body, entries)
    path.write_text(new_text, encoding="utf-8")

    validation_failure: str | None = None
    try:
        validated_document = parse_plan(
            path,
            plan_kind=plan_kind,
            parse_mode="draft",
            plan_id_override=plan_id,
        )
        if not validated_document.manifest_entries:
            validation_failure = "emitted manifest was not present in the parsed document"
    except PlanParseError as exc:
        validation_failure = str(exc)

    if validation_failure is not None:
        path.write_text(body, encoding="utf-8")
        if "\n## Yolo Fallbacks\n" not in f"\n{body}":
            _append_yolo_fallback(
                path,
                by_actor=by_actor,
                reason=(f"synthesized manifest failed draft validation: {validation_failure}"),
            )
        return "fallback_force_approve"

    return "fresh"


def _synthesize_entry(
    plan_id: str, section: PlanSection, dependencies: tuple[str, ...]
) -> dict[str, object]:
    category = _extract_category(section.title)
    title = _clean_title(section.title) or section.section_id
    if section.acceptance_items:
        first = section.acceptance_items[0]
        validation = first.artifact_ref or first.prose or title
    else:
        validation = title
    labels = [
        f"covers:{plan_id}:{section.section_id}:{item.item_id}" for item in section.acceptance_items
    ]
    entry: dict[str, object] = {
        "title": title,
        "category": category,
        "task_type": _DEFAULT_TASK_TYPE,
        "depends_on": list(dependencies),
        "validation_criteria": validation,
        "labels": labels,
        "tdd": category in TDD_ELIGIBLE_CATEGORIES,
        "source_section": section.section_id,
    }
    if category == "code":
        domain = _implementation_domain_for(section, title, validation)
        entry["implementation_domain"] = domain
    else:
        entry["assigned_agent"] = _agent_for(category, section, title, validation)
    return entry


def _synthesized_dependencies(
    document: PlanDocument,
    deliverables: list[PlanSection],
) -> dict[str, tuple[str, ...]]:
    section_by_id = {section.section_id: section for section in document.sections}
    deliverable_ids = {section.section_id for section in deliverables}
    by_phase = deliverables_by_phase(document, deliverables)
    dependencies_by_section: dict[str, tuple[str, ...]] = {}

    for section in deliverables:
        dependencies_by_section[section.section_id] = _resolve_dependency_refs(
            list(section_dependency_refs(section, section_by_id)),
            section_id=section.section_id,
            section_by_id=section_by_id,
            deliverable_ids=deliverable_ids,
            deliverables_by_phase=by_phase,
            require_list=False,
        )
    return dependencies_by_section


def section_dependency_refs(
    section: PlanSection,
    section_by_id: Mapping[str, PlanSection],
) -> tuple[str, ...]:
    """Return a section's ``(depends: ...)`` refs plus those inherited from its headings.

    A ``(depends: ...)`` annotation on a phase heading (or any heading between
    the phase and the deliverable) applies to every deliverable under it.
    """
    refs: list[str] = list(extract_section_dependencies(section.title))
    current = section
    while current.parent_id is not None:
        parent = section_by_id.get(current.parent_id)
        if parent is None:
            break
        for ref in extract_section_dependencies(parent.title):
            if ref not in refs:
                refs.append(ref)
        if _PHASE_REF_RE.match(parent.section_id):
            break
        current = parent
    return tuple(refs)


def resolve_dependency_ref(
    ref: str,
    *,
    deliverable_ids: set[str],
    deliverables_by_phase: Mapping[str, list[str]],
) -> tuple[str, ...]:
    """Resolve one dependency ref to deliverable ids; empty when it resolves to nothing."""
    if ref in deliverable_ids:
        return (ref,)
    if _PHASE_REF_RE.match(ref):
        return tuple(deliverables_by_phase.get(ref, ()))
    return ()


def _resolve_dependency_refs(
    raw_refs: object,
    *,
    section_id: str,
    section_by_id: Mapping[str, PlanSection],
    deliverable_ids: set[str],
    deliverables_by_phase: Mapping[str, list[str]],
    require_list: bool,
) -> tuple[str, ...]:
    if require_list and not isinstance(raw_refs, list):
        raise ManifestSynthesisError(f"routing decision for {section_id!r} has invalid depends_on")
    if not isinstance(raw_refs, list):
        raise ManifestSynthesisError(f"dependencies for {section_id!r} must be an array")

    resolved: list[str] = []
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, str) or not raw_ref:
            raise ManifestSynthesisError(f"empty dependency reference in section {section_id!r}")
        candidates = resolve_dependency_ref(
            raw_ref,
            deliverable_ids=deliverable_ids,
            deliverables_by_phase=deliverables_by_phase,
        )
        if not candidates:
            if _PHASE_REF_RE.match(raw_ref) and raw_ref in section_by_id:
                raise ManifestSynthesisError(
                    f"phase dependency {raw_ref!r} in section {section_id!r} "
                    "has no deliverable sections"
                )
            raise ManifestSynthesisError(
                f"unknown dependency reference {raw_ref!r} in section {section_id!r}"
            )
        for candidate in candidates:
            if candidate == section_id:
                raise ManifestSynthesisError(f"section {section_id!r} cannot depend on itself")
            if candidate not in resolved:
                resolved.append(candidate)
    return tuple(resolved)


def deliverables_by_phase(
    document: PlanDocument,
    deliverables: list[PlanSection],
) -> dict[str, list[str]]:
    """Group deliverable section ids by their enclosing ``P<N>`` phase."""
    section_by_id = {section.section_id: section for section in document.sections}
    by_phase: dict[str, list[str]] = {}
    for deliverable in deliverables:
        phase_id = _phase_parent_id(deliverable, section_by_id)
        if phase_id is not None:
            by_phase.setdefault(phase_id, []).append(deliverable.section_id)
    return by_phase


def _phase_parent_id(
    section: PlanSection,
    section_by_id: dict[str, PlanSection],
) -> str | None:
    current = section
    while current.parent_id is not None:
        parent = section_by_id.get(current.parent_id)
        if parent is None:
            return None
        if _PHASE_REF_RE.match(parent.section_id):
            return parent.section_id
        current = parent
    return None


def _agent_for(
    category: str,
    section: PlanSection,
    title: str,
    validation: str,
) -> str:
    if category == "code":
        return AGENT_BY_IMPLEMENTATION_DOMAIN[
            _implementation_domain_for(section, title, validation)
        ]
    return _AGENT_BY_CATEGORY.get(category, _DEFAULT_AGENT_FALLBACK)


def _implementation_domain_for(section: PlanSection, title: str, validation: str) -> str:
    signal_text = " ".join(
        [
            section.title,
            title,
            validation,
            *[item.artifact_ref for item in section.acceptance_items],
        ]
    )
    frontend = _FRONTEND_SIGNAL_RE.search(signal_text) is not None
    backend = _BACKEND_SIGNAL_RE.search(signal_text) is not None
    if frontend and backend:
        return "fullstack"
    if frontend:
        return "frontend"
    return "backend"


def _extract_category(title: str) -> str:
    match = _CATEGORY_RE.search(title)
    if match is None:
        return _DEFAULT_CATEGORY
    category = match.group("value")
    if category not in DEVELOPMENT_FORWARD_LEAF_CATEGORIES:
        return _DEFAULT_CATEGORY
    return category


def _clean_title(title: str) -> str:
    return _TITLE_BRACKET_RE.sub("", title).strip()


def _write_manifest_section(body: str, entries: list[dict[str, object]]) -> str:
    yaml_block = yaml.safe_dump(entries, sort_keys=False, default_flow_style=False)
    body_normalized = body.rstrip() + "\n"
    return (
        f"{body_normalized}\n## M1 Task Manifest\n"
        f"`kind: manifest`\n\n```yaml\n{yaml_block.rstrip()}\n```\n"
    )


def _has_manifest_section(raw: str) -> bool:
    lines = raw.splitlines()
    fence_mask, _ = compute_fence_mask(lines)
    for index, line in enumerate(lines):
        if fence_mask[index]:
            continue
        if _HEADING_LINE_RE.match(line):
            if _next_kind_directive(lines, fence_mask, index + 1) == "manifest":
                return True
    return False


def _strip_manifest_section(raw: str) -> str:
    lines = raw.splitlines()
    fence_mask, _ = compute_fence_mask(lines)
    manifest_start = -1
    manifest_level = -1

    for index, line in enumerate(lines):
        if fence_mask[index]:
            continue
        if manifest_start < 0:
            heading_match = _HEADING_LINE_RE.match(line)
            if heading_match is None:
                continue
            if _next_kind_directive(lines, fence_mask, index + 1) == "manifest":
                manifest_start = index
                manifest_level = len(heading_match.group("marks"))
            continue
        # We are scanning for the section's end after the manifest heading.
        heading_match = _HEADING_LINE_RE.match(line)
        if heading_match is not None and len(heading_match.group("marks")) <= manifest_level:
            kept = lines[:manifest_start] + lines[index:]
            return "\n".join(kept).rstrip() + "\n"

    if manifest_start < 0:
        return raw
    kept = lines[:manifest_start]
    text = "\n".join(kept).rstrip()
    return text + "\n" if text else ""


def _next_kind_directive(lines: list[str], fence_mask: list[bool], start_index: int) -> str | None:
    for index in range(start_index, len(lines)):
        if fence_mask[index]:
            continue
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_LINE_RE.match(line):
            return None
        match = KIND_LINE_RE.match(stripped)
        if match is not None:
            return match.group("kind")
        return None
    return None


def _append_yolo_fallback(path: Path, *, by_actor: str, reason: str) -> None:
    timestamp = datetime.now(UTC).isoformat()
    audit = (
        "\n## Yolo Fallbacks\n"
        "`kind: framing`\n\n"
        f"- by: {by_actor}\n"
        f"- at: {timestamp}\n"
        f"- reason: {reason}\n"
    )
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        existing = ""
    trailing = existing.rstrip() + "\n" if existing else ""
    try:
        path.write_text(trailing + audit, encoding="utf-8")
    except OSError:
        return
