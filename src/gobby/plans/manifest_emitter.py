"""Manifest emitter for the yolo cap-exhausted fallback path (§2.21a).

Idempotent stub-manifest writer. NEVER raises: on unsalvageable plans the
emitter appends a ``## Yolo Fallbacks`` audit section and returns
``"fallback_force_approve"`` so dispatch can advance lifecycle deterministically.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from gobby.plans.parser import (
    Kind,
    PlanDocument,
    PlanKind,
    PlanParseError,
    PlanSection,
    extract_section_dependencies,
    parse_plan,
    resolve_plan_id,
)
from gobby.storage.tasks import TDD_ELIGIBLE_CATEGORIES

EmitOutcome = Literal[
    "fresh",
    "replaced_malformed",
    "noop_existing_valid",
    "fallback_force_approve",
]

__all__ = ["EmitOutcome", "emit_stub_manifest"]

_HEADING_LINE_RE = re.compile(r"^(?P<marks>#{2,6})\s+")
_KIND_DIRECTIVE_RE = re.compile(r"^`?kind:\s*(?P<value>[a-z_]+)`?\s*$")
_CATEGORY_RE = re.compile(r"\[category:\s*(?P<value>[a-z_]+)\]")
_TITLE_BRACKET_RE = re.compile(r"\s*(?:\[category:[^\]]+\]|\(depends:[^)]+\))")
_FENCE_OPEN_RE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})")
_DEFAULT_CATEGORY = "code"
_AGENT_BY_CATEGORY: dict[str, str] = {
    "code": "backend-developer",
    "refactor": "backend-developer",
    "test": "test-architect",
}
_DEFAULT_AGENT_FALLBACK = "backend-developer"
_DEFAULT_TASK_TYPE = "feature"


def emit_stub_manifest(
    plan_path: str | Path,
    *,
    by_actor: str = "dispatcher",
    plan_kind: PlanKind = PlanKind.implementation,
) -> EmitOutcome:
    """Idempotent manifest emitter — see §2.21a.

    Sequence:
    1. Parse in ``parse_mode="draft"``.
    2. Valid manifest already present → ``"noop_existing_valid"``.
    3. Malformed manifest present → replace with synthesized one →
       ``"replaced_malformed"``.
    4. No manifest → synthesize one → ``"fresh"``.
    5. Anything unsalvageable → append a ``## Yolo Fallbacks`` audit section →
       ``"fallback_force_approve"``. The function NEVER raises.
    """

    path = Path(plan_path)
    try:
        return _emit(path, by_actor=by_actor, plan_kind=plan_kind)
    except Exception as exc:  # noqa: BLE001 — yolo invariant: absorb everything
        _append_yolo_fallback(path, by_actor=by_actor, reason=f"emitter exception: {exc!r}")
        return "fallback_force_approve"


def _emit(path: Path, *, by_actor: str, plan_kind: PlanKind) -> EmitOutcome:
    raw = path.read_text(encoding="utf-8")

    draft_doc: PlanDocument | None = None
    draft_error: PlanParseError | None = None
    try:
        draft_doc = parse_plan(path, plan_kind=plan_kind, parse_mode="draft")
    except PlanParseError as exc:
        draft_error = exc

    if draft_doc is not None:
        if draft_doc.manifest_entries:
            try:
                parse_plan(path, plan_kind=plan_kind, parse_mode="expansion")
                return "noop_existing_valid"
            except PlanParseError:
                return _replace_existing_manifest(path, raw, plan_kind=plan_kind, by_actor=by_actor)
        return _emit_fresh(path, raw, draft_doc, by_actor=by_actor, plan_kind=plan_kind)

    if _has_manifest_section(raw):
        return _replace_existing_manifest(path, raw, plan_kind=plan_kind, by_actor=by_actor)

    assert draft_error is not None
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
) -> EmitOutcome:
    stripped = _strip_manifest_section(raw)
    path.write_text(stripped, encoding="utf-8")

    try:
        stripped_doc = parse_plan(path, plan_kind=plan_kind, parse_mode="draft")
    except PlanParseError as exc:
        path.write_text(raw, encoding="utf-8")
        _append_yolo_fallback(
            path,
            by_actor=by_actor,
            reason=f"plan still malformed after stripping manifest: {exc}",
        )
        return "fallback_force_approve"

    outcome = _emit_fresh(path, stripped, stripped_doc, by_actor=by_actor, plan_kind=plan_kind)
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
) -> EmitOutcome:
    plan_id = resolve_plan_id(document.plan_id)
    deliverables = [section for section in document.sections if section.kind is Kind.deliverable]
    if not deliverables:
        _append_yolo_fallback(
            path,
            by_actor=by_actor,
            reason="plan has no kind: deliverable sections to synthesize a manifest from",
        )
        return "fallback_force_approve"

    entries = [_synthesize_entry(plan_id, section) for section in deliverables]
    new_text = _write_manifest_section(body, entries)
    path.write_text(new_text, encoding="utf-8")

    try:
        parse_plan(path, plan_kind=plan_kind, parse_mode="expansion")
    except PlanParseError as exc:
        _append_yolo_fallback(
            path,
            by_actor=by_actor,
            reason=f"synthesized manifest failed expansion validation: {exc}",
        )
        return "fallback_force_approve"

    return "fresh"


def _synthesize_entry(plan_id: str, section: PlanSection) -> dict[str, object]:
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
    return {
        "title": title,
        "category": category,
        "task_type": _DEFAULT_TASK_TYPE,
        "depends_on": list(extract_section_dependencies(section.title)),
        "validation_criteria": validation,
        "labels": labels,
        "assigned_agent": _agent_for(category),
        "tdd": category in TDD_ELIGIBLE_CATEGORIES,
        "source_section": section.section_id,
    }


def _agent_for(category: str) -> str:
    return _AGENT_BY_CATEGORY.get(category, _DEFAULT_AGENT_FALLBACK)


def _extract_category(title: str) -> str:
    match = _CATEGORY_RE.search(title)
    if match is None:
        return _DEFAULT_CATEGORY
    return match.group("value")


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
    in_fence = False
    fence_marker = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            continue
        opener = _FENCE_OPEN_RE.match(line)
        if opener is not None:
            in_fence = True
            fence_marker = opener.group("marker")[:3]
            continue
        if _HEADING_LINE_RE.match(line):
            if _next_kind_directive(lines, index + 1) == "manifest":
                return True
    return False


def _strip_manifest_section(raw: str) -> str:
    lines = raw.splitlines()
    in_fence = False
    fence_marker = ""
    manifest_start = -1
    manifest_level = -1

    for index, line in enumerate(lines):
        stripped = line.strip()
        if manifest_start < 0:
            if in_fence:
                if stripped.startswith(fence_marker):
                    in_fence = False
                continue
            opener = _FENCE_OPEN_RE.match(line)
            if opener is not None:
                in_fence = True
                fence_marker = opener.group("marker")[:3]
                continue
            heading_match = _HEADING_LINE_RE.match(line)
            if heading_match is None:
                continue
            if _next_kind_directive(lines, index + 1) == "manifest":
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


def _next_kind_directive(lines: list[str], start_index: int) -> str | None:
    in_fence = False
    fence_marker = ""
    for index in range(start_index, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            continue
        if not stripped:
            continue
        opener = _FENCE_OPEN_RE.match(line)
        if opener is not None:
            in_fence = True
            fence_marker = opener.group("marker")[:3]
            continue
        if _HEADING_LINE_RE.match(line):
            return None
        match = _KIND_DIRECTIVE_RE.match(stripped)
        if match is not None:
            return match.group("value")
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
