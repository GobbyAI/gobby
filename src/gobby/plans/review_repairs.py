"""Typed, mechanically applicable repairs attached to plan-review findings.

This module must not import ``gobby.plans.review_findings``; that module
imports this one to validate and project the ``repairs`` field.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.plans.manifest_emitter import deliverables_by_phase
from gobby.plans.parser import (
    Kind,
    PlanDocument,
    PlanSection,
    compute_fence_mask,
    extract_section_dependencies,
    strip_section_dependencies,
)
from gobby.plans.review_evidence_io import build_section_manifest, parse_plan_bytes
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    SectionHash,
)
from gobby.plans.symbol_targets import parse_symbol_targets, parse_target_line

REPAIR_KINDS = ("add_targets", "add_dependency", "add_acceptance")
REPAIR_KINDS_BY_CATEGORY: Mapping[str, frozenset[str]] = {
    "traceability": frozenset({"add_targets", "add_acceptance"}),
    "bad-sequencing": frozenset({"add_dependency"}),
    "gobby-format": frozenset(REPAIR_KINDS),
    "weak-testability": frozenset({"add_acceptance"}),
}
_PAYLOAD_KEY_BY_KIND = {
    "add_targets": "entries",
    "add_dependency": "on",
    "add_acceptance": "items",
}
_ACCEPTANCE_ITEM_KEYS = frozenset({"prose", "artifact"})
_ARTIFACT_RE = re.compile(r"^(file|symbol|test|behavior):\s*\S")

_NONEMPTY_STRING_SCHEMA = {"type": "string", "minLength": 1}
REPAIR_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "add_targets"},
                    "section_id": _NONEMPTY_STRING_SCHEMA,
                    "entries": {
                        "type": "array",
                        "minItems": 1,
                        "items": _NONEMPTY_STRING_SCHEMA,
                    },
                },
                "required": ["kind", "section_id", "entries"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "add_dependency"},
                    "section_id": _NONEMPTY_STRING_SCHEMA,
                    "on": {
                        "type": "array",
                        "minItems": 1,
                        "items": _NONEMPTY_STRING_SCHEMA,
                        "uniqueItems": True,
                    },
                },
                "required": ["kind", "section_id", "on"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "add_acceptance"},
                    "section_id": _NONEMPTY_STRING_SCHEMA,
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "prose": _NONEMPTY_STRING_SCHEMA,
                                "artifact": _NONEMPTY_STRING_SCHEMA,
                            },
                            "required": ["prose", "artifact"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "section_id", "items"],
                "additionalProperties": False,
            },
        ]
    },
}


def validate_finding_repairs(
    raw: object,
    *,
    prefix: str,
    category: str,
    section_ids: set[str],
) -> list[dict[str, object]]:
    """Validate one finding's ``repairs`` value and return the canonical list."""
    field = f"{prefix}.repairs"
    allowed = REPAIR_KINDS_BY_CATEGORY.get(category)
    if allowed is None:
        raise _invalid(f"{field} is not allowed for category {category!r}")
    if not isinstance(raw, list) or not raw:
        raise _invalid(f"{field} must be a non-empty array")
    canonical: list[dict[str, object]] = []
    for index, entry in enumerate(raw):
        canonical.append(
            _validate_repair(
                entry,
                prefix=f"{field}[{index}]",
                allowed=allowed,
                section_ids=section_ids,
            )
        )
    return canonical


def _validate_repair(
    raw: object,
    *,
    prefix: str,
    allowed: frozenset[str],
    section_ids: set[str],
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise _invalid(f"{prefix} must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in REPAIR_KINDS:
        raise _invalid(f"{prefix}.kind must be one of {', '.join(REPAIR_KINDS)}")
    if kind not in allowed:
        raise _invalid(f"{prefix}.kind {kind!r} is not allowed for this finding category")
    payload_key = _PAYLOAD_KEY_BY_KIND[kind]
    expected_keys = {"kind", "section_id", payload_key}
    unexpected = sorted(set(raw) - expected_keys)
    if unexpected:
        raise _invalid(f"{prefix} has unexpected keys for {kind}: {', '.join(unexpected)}")
    section_id = raw.get("section_id")
    if not isinstance(section_id, str) or not section_id.strip():
        raise _invalid(f"{prefix}.section_id must be a non-empty string")
    if section_id not in section_ids:
        raise _invalid(f"{prefix}.section_id is absent from the evidence manifest")
    if payload_key not in raw:
        raise _invalid(f"{prefix} requires {payload_key} for {kind}")
    payload_prefix = f"{prefix}.{payload_key}"
    payload = raw[payload_key]
    if not isinstance(payload, list) or not payload:
        raise _invalid(f"{payload_prefix} must be a non-empty array")

    value: list[object]
    if kind == "add_targets":
        value = list(_validate_entries(payload, prefix=payload_prefix, section_id=section_id))
    elif kind == "add_dependency":
        value = list(
            _validate_dependency_refs(
                payload,
                prefix=payload_prefix,
                section_id=section_id,
                section_ids=section_ids,
            )
        )
    else:
        value = list(_validate_acceptance_items(payload, prefix=payload_prefix))
    return {"kind": kind, "section_id": section_id, payload_key: value}


def _validate_entries(payload: list[object], *, prefix: str, section_id: str) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(payload):
        entry_prefix = f"{prefix}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            raise _invalid(f"{entry_prefix} must be a non-empty string")
        text = entry.strip()
        targets, issues = parse_target_line(text, section_id)
        if issues:
            raise _invalid(f"{entry_prefix} is not a valid target: {issues[0].message}")
        if len(targets) != 1:
            raise _invalid(f"{entry_prefix} must name exactly one target")
        reference = targets[0].reference
        if reference in seen:
            raise _invalid(f"{entry_prefix} duplicates target `{reference}`")
        seen.add(reference)
        entries.append(text)
    return entries


def _validate_dependency_refs(
    payload: list[object],
    *,
    prefix: str,
    section_id: str,
    section_ids: set[str],
) -> list[str]:
    refs: list[str] = []
    for index, ref in enumerate(payload):
        ref_prefix = f"{prefix}[{index}]"
        if not isinstance(ref, str) or not ref.strip():
            raise _invalid(f"{ref_prefix} must be a non-empty string")
        if ref not in section_ids:
            raise _invalid(f"{ref_prefix} is absent from the evidence manifest")
        if ref == section_id:
            raise _invalid(f"{ref_prefix} must not depend on its own section")
        if ref in refs:
            raise _invalid(f"{ref_prefix} duplicates dependency {ref!r}")
        refs.append(ref)
    return refs


def _validate_acceptance_items(payload: list[object], *, prefix: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        item_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            raise _invalid(f"{item_prefix} must be an object")
        if set(item) != _ACCEPTANCE_ITEM_KEYS:
            raise _invalid(f"{item_prefix} must have exactly the keys prose and artifact")
        prose = _single_line(item["prose"], prefix=f"{item_prefix}.prose")
        artifact = _single_line(item["artifact"], prefix=f"{item_prefix}.artifact")
        if _ARTIFACT_RE.match(artifact) is None:
            raise _invalid(
                f"{item_prefix}.artifact must start with file:, symbol:, test:, or behavior:"
            )
        items.append({"prose": prose, "artifact": artifact})
    return items


def _single_line(value: object, *, prefix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{prefix} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise _invalid(f"{prefix} must be a single line")
    return value.strip()


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_round_result", message)


# --- Applying accepted repairs -------------------------------------------------

_TARGET_LINE_RE = re.compile(r"^\s*Targets?\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_ACCEPTANCE_MARKER_RE = re.compile(r"^\s*\*\*Acceptance:\*\*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_ACCEPTANCE_BULLET_RE = re.compile(
    r"^(?P<indent>\s*)-\s+(?P<item_id>[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+)"
    r"(?P<separator>\s+(?:-|—)\s+)(?P<prose>.*)$"
)
_DEFAULT_SEPARATOR = " - "


@dataclass(frozen=True)
class Repair:
    """One accepted, validated repair bound to the finding that proposed it."""

    finding_id: str
    kind: str
    section_id: str
    entries: tuple[str, ...] = ()
    on: tuple[str, ...] = ()
    items: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RepairOutcome:
    """Result of applying repairs to plan bytes without touching the file."""

    updated: bytes
    applied: list[dict[str, object]]
    skipped: list[dict[str, str]]
    diff: str


def select_accepted_repairs(
    evidence: PlanReviewEvidence,
    accepted_ids: Sequence[str],
) -> tuple[list[Repair], list[dict[str, str]]]:
    """Resolve accepted finding ids against the evidence's persisted findings."""
    round_result = evidence.round_result or {}
    raw_findings = round_result.get("findings")
    findings_by_id: dict[str, Mapping[str, object]] = {}
    if isinstance(raw_findings, list):
        for raw in raw_findings:
            if isinstance(raw, Mapping):
                findings_by_id[str(raw.get("finding_id"))] = raw
    unknown = [finding_id for finding_id in accepted_ids if finding_id not in findings_by_id]
    if unknown:
        raise ReviewEvidenceError(
            "unknown_finding_id",
            f"accepted finding ids are absent from the round result: {', '.join(unknown)}",
            details={"unknown_finding_ids": unknown},
        )
    repairs: list[Repair] = []
    skipped: list[dict[str, str]] = []
    for finding_id in dict.fromkeys(accepted_ids):
        raw_repairs = findings_by_id[finding_id].get("repairs")
        if not isinstance(raw_repairs, list) or not raw_repairs:
            skipped.append({"finding_id": finding_id, "reason": "prose_only"})
            continue
        repairs.extend(_repair_from_mapping(finding_id, raw) for raw in raw_repairs)
    return repairs, skipped


def _repair_from_mapping(finding_id: str, raw: object) -> Repair:
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_repair", f"finding {finding_id} carries a malformed repair"
        )
    kind = str(raw.get("kind"))
    section_id = str(raw.get("section_id"))
    if kind == "add_targets":
        return Repair(
            finding_id=finding_id,
            kind=kind,
            section_id=section_id,
            entries=tuple(str(entry) for entry in _list(raw.get("entries"))),
        )
    if kind == "add_dependency":
        return Repair(
            finding_id=finding_id,
            kind=kind,
            section_id=section_id,
            on=tuple(str(ref) for ref in _list(raw.get("on"))),
        )
    if kind == "add_acceptance":
        items = tuple(
            (str(item.get("prose")), str(item.get("artifact")))
            for item in _list(raw.get("items"))
            if isinstance(item, Mapping)
        )
        return Repair(finding_id=finding_id, kind=kind, section_id=section_id, items=items)
    raise ReviewEvidenceError(
        "invalid_repair", f"finding {finding_id} has unknown repair kind {kind}"
    )


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def apply_repairs(
    current: bytes,
    *,
    plan_name: str,
    repairs: Sequence[Repair],
) -> RepairOutcome:
    """Apply repairs to ``current`` and return the updated bytes plus a ledger."""
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewEvidenceError(
            "invalid_plan_encoding", f"plan must be valid UTF-8: {exc}"
        ) from exc
    lines = text.splitlines()
    trailing = "\n" if text.endswith("\n") else ""
    if "\n".join(lines) + trailing != text:
        raise ReviewEvidenceError(
            "unsupported_plan_text",
            "plan must use bare LF line endings with no exotic line separators",
        )

    before = parse_plan_bytes(plan_name, current)
    manifest_before = build_section_manifest(current)
    targets_before, issues_before = parse_symbol_targets(before)
    sections_by_id = {section.section_id: section for section in before.sections}
    deliverables = [s for s in before.sections if s.kind is Kind.deliverable]
    phase_of = {
        deliverable_id: phase_id
        for phase_id, ids in deliverables_by_phase(before, deliverables).items()
        for deliverable_id in ids
    }
    mask, _unclosed = compute_fence_mask(lines)

    grouped: dict[str, list[Repair]] = {}
    for repair in repairs:
        section = sections_by_id.get(repair.section_id)
        if section is None:
            raise ReviewEvidenceError(
                "repair_section_missing",
                f"repair targets section {repair.section_id}, which is absent from the plan",
            )
        if section.kind is not Kind.deliverable:
            raise ReviewEvidenceError(
                "repair_section_not_deliverable",
                f"repair targets section {repair.section_id}, which is not a deliverable",
            )
        grouped.setdefault(repair.section_id, []).append(repair)

    ledger = _Ledger()
    edits: list[_Edit] = []
    for section_id, section_repairs in grouped.items():
        surgeon = _SectionSurgeon(
            lines=lines,
            mask=mask,
            section=sections_by_id[section_id],
            existing_targets={t.reference for t in targets_before if t.section_id == section_id},
            phase_of=phase_of,
        )
        edits.extend(surgeon.edits(section_repairs, ledger))

    updated_lines = list(lines)
    for edit in sorted(edits, key=lambda item: item.index, reverse=True):
        updated_lines[edit.index : edit.index + edit.delete_count] = edit.new_lines
    updated_text = "\n".join(updated_lines) + trailing
    updated = updated_text.encode("utf-8")
    if updated == current:
        return RepairOutcome(
            updated=current, applied=ledger.applied, skipped=ledger.skipped, diff=""
        )

    after = _parse_repaired(plan_name, updated)
    _verify_section_hashes(manifest_before, build_section_manifest(updated), set(grouped))
    _targets_after, issues_after = parse_symbol_targets(after)
    if len(issues_after) > len(issues_before):
        raise ReviewEvidenceError(
            "invalid_repair",
            "repairs introduced target issues: "
            + "; ".join(issue.message for issue in issues_after[len(issues_before) :]),
        )
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=plan_name,
            tofile=plan_name,
        )
    )
    return RepairOutcome(updated=updated, applied=ledger.applied, skipped=ledger.skipped, diff=diff)


def _parse_repaired(plan_name: str, updated: bytes) -> PlanDocument:
    try:
        return parse_plan_bytes(plan_name, updated)
    except ReviewEvidenceError as exc:
        raise ReviewEvidenceError(
            "invalid_repair", f"plan does not parse after repairs: {exc}"
        ) from exc


def _verify_section_hashes(
    before: Sequence[SectionHash],
    after: Sequence[SectionHash],
    repaired: set[str],
) -> None:
    before_by_id = {entry.section_id: entry.section_hash for entry in before}
    after_by_id = {entry.section_id: entry.section_hash for entry in after}
    if set(before_by_id) != set(after_by_id):
        raise ReviewEvidenceError("invalid_repair", "repairs changed the plan's section set")
    changed = {key for key, value in after_by_id.items() if before_by_id[key] != value}
    if not changed <= repaired:
        raise ReviewEvidenceError(
            "invalid_repair",
            f"repairs changed unrepaired sections: {', '.join(sorted(changed - repaired))}",
        )


@dataclass(frozen=True)
class _Edit:
    index: int
    delete_count: int
    new_lines: list[str]


class _Ledger:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []
        self.skipped: list[dict[str, str]] = []

    def record(self, repair: Repair, added: list[str]) -> None:
        if added:
            self.applied.append(
                {
                    "finding_id": repair.finding_id,
                    "kind": repair.kind,
                    "section_id": repair.section_id,
                    "added": added,
                }
            )
        else:
            self.skipped.append({"finding_id": repair.finding_id, "reason": "already_present"})


class _SectionSurgeon:
    """Compute the edits for one deliverable section from the original lines."""

    def __init__(
        self,
        *,
        lines: list[str],
        mask: list[bool],
        section: PlanSection,
        existing_targets: set[str],
        phase_of: Mapping[str, str],
    ) -> None:
        self.lines = lines
        self.mask = mask
        self.section = section
        self.existing_targets = existing_targets
        self.phase_of = phase_of
        self.start = section.source_span[0] - 1
        self.end = section.source_span[1] - 1
        self.acceptance_index = self._acceptance_marker_index()

    def edits(self, repairs: Sequence[Repair], ledger: _Ledger) -> list[_Edit]:
        target_bullets: list[str] = []
        dependency_refs: list[str] = list(extract_section_dependencies(self.lines[self.start]))
        existing_dependency_count = len(dependency_refs)
        item_bodies: list[str] = []
        for repair in repairs:
            if repair.kind == "add_targets":
                ledger.record(repair, self._new_target_bullets(repair, target_bullets))
            elif repair.kind == "add_dependency":
                ledger.record(repair, self._new_dependency_refs(repair, dependency_refs))
            else:
                ledger.record(repair, self._new_acceptance_bodies(repair, item_bodies))
        edits: list[_Edit] = []
        if len(dependency_refs) > existing_dependency_count:
            edits.append(self._heading_edit(dependency_refs))
        if target_bullets:
            edits.append(self._targets_edit(target_bullets))
        if item_bodies:
            edits.append(self._acceptance_edit(item_bodies))
        return edits

    # -- add_targets ----------------------------------------------------------

    def _new_target_bullets(self, repair: Repair, bullets: list[str]) -> list[str]:
        added: list[str] = []
        for entry in repair.entries:
            targets, issues = parse_target_line(entry, self.section.section_id)
            if issues or len(targets) != 1:
                raise ReviewEvidenceError(
                    "invalid_repair",
                    f"finding {repair.finding_id} carries an unparseable target entry: {entry}",
                )
            target = targets[0]
            if target.reference in self.existing_targets:
                continue
            self.existing_targets.add(target.reference)
            bullet = f"- `{target.reference}`"
            if target.wildcard:
                bullet += f" — scope-reason: {target.scope_reason}"
            bullets.append(bullet)
            added.append(target.reference)
        return added

    def _targets_edit(self, bullets: list[str]) -> _Edit:
        tail = self._target_block_end()
        if tail is not None:
            return _Edit(index=tail, delete_count=0, new_lines=bullets)
        index = self._kind_line_index() + 1
        new_lines = ["", "Targets:", *bullets]
        if index > self.end or self.lines[index].strip():
            new_lines.append("")
        return _Edit(index=index, delete_count=0, new_lines=new_lines)

    def _target_block_end(self) -> int | None:
        limit = self.acceptance_index if self.acceptance_index is not None else self.end + 1
        last_header: int | None = None
        for index in range(self.start + 1, limit):
            if not self.mask[index] and _TARGET_LINE_RE.match(self.lines[index]):
                last_header = index
        if last_header is None:
            return None
        index = last_header + 1
        while index < limit:
            if self.mask[index]:
                index += 1
                continue
            candidate = self.lines[index]
            stripped = candidate.strip()
            if not stripped:
                break
            if _TARGET_LINE_RE.match(candidate) or _ACCEPTANCE_MARKER_RE.match(candidate):
                break
            if stripped.startswith("#") or stripped.startswith("`kind:"):
                break
            if _BULLET_RE.match(candidate) or "`" in candidate or "/" in candidate:
                index += 1
                continue
            break
        return index

    def _kind_line_index(self) -> int:
        for index in range(self.start + 1, self.end + 1):
            if not self.mask[index] and self.lines[index].strip().startswith("`kind:"):
                return index
        return self.start

    # -- add_dependency -------------------------------------------------------

    def _new_dependency_refs(self, repair: Repair, merged: list[str]) -> list[str]:
        added: list[str] = []
        for ref in repair.on:
            if ref in merged or self.phase_of.get(ref) in merged:
                continue
            merged.append(ref)
            added.append(ref)
        return added

    def _heading_edit(self, refs: Sequence[str]) -> _Edit:
        heading = strip_section_dependencies(self.lines[self.start]).rstrip()
        return _Edit(
            index=self.start,
            delete_count=1,
            new_lines=[f"{heading} (depends: {', '.join(refs)})"],
        )

    # -- add_acceptance -------------------------------------------------------

    def _new_acceptance_bodies(self, repair: Repair, bodies: list[str]) -> list[str]:
        existing = {_normalize_body(item.prose) for item in self.section.acceptance_items}
        existing.update(_normalize_body(body) for body in bodies)
        added: list[str] = []
        for prose, artifact in repair.items:
            body = f"{prose.rstrip('.')}. {artifact.rstrip('.')}."
            if _normalize_body(body) in existing:
                continue
            existing.add(_normalize_body(body))
            bodies.append(body)
            added.append(body)
        return added

    def _acceptance_edit(self, bodies: list[str]) -> _Edit:
        if self.acceptance_index is None:
            raise ReviewEvidenceError(
                "invalid_repair",
                f"section {self.section.section_id} has no **Acceptance:** block",
            )
        items = self.section.acceptance_items
        if not items:
            insert_at = self.acceptance_index + 1
            next_number = 1
            separator = _DEFAULT_SEPARATOR
            indent = ""
        else:
            last = max(items, key=lambda item: item.source_line)
            last_index = last.source_line - 1
            insert_at = self._item_tail_end(last_index) + 1
            next_number = max(_item_number(item.item_id) for item in items) + 1
            match = _ACCEPTANCE_BULLET_RE.match(self.lines[last_index])
            separator = match.group("separator") if match else _DEFAULT_SEPARATOR
            indent = match.group("indent") if match else ""
        new_lines = []
        for offset, body in enumerate(bodies):
            item_id = f"{self.section.section_id}.{next_number + offset}"
            new_lines.append(f"{indent}- {item_id}{separator}{body}")
        return _Edit(index=insert_at, delete_count=0, new_lines=new_lines)

    def _item_tail_end(self, bullet_index: int) -> int:
        tail = bullet_index
        crossed_blank = False
        for index in range(bullet_index + 1, self.end + 1):
            if self.mask[index]:
                tail = index
                continue
            line = self.lines[index]
            if not line.strip():
                crossed_blank = True
                continue
            if crossed_blank and not line[:1].isspace():
                break
            tail = index
        return tail

    def _acceptance_marker_index(self) -> int | None:
        for index in range(self.start + 1, self.end + 1):
            if not self.mask[index] and _ACCEPTANCE_MARKER_RE.match(self.lines[index]):
                return index
        return None


def _item_number(item_id: str) -> int:
    suffix = item_id.rsplit(".", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def _normalize_body(body: str) -> str:
    return " ".join(body.split()).rstrip(".").strip().lower()
