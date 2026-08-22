"""Plan-file hashing and checkpoint IO for review evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import cast

import yaml

from gobby.plans.parser import (
    PLAN_HEADING_REGEX,
    ParseMode,
    PlanDocument,
    PlanKind,
    compute_fence_mask,
    parse_plan,
)
from gobby.plans.review_evidence_models import (
    ReviewEvidenceError,
    SectionHash,
    canonical_json_bytes,
    validate_round_result,
)

PREAMBLE_SECTION_ID = "__preamble__"
COORDINATOR_OWNED_SECTIONS = ("Task Mapping", "M1", "V1")
CHECKPOINT_FENCE = "```json plan-review-round"
_HEADING_RE = re.compile(r"^(?P<marks>#{2,6})[ \t]+(?P<title>.*?)(?:[ \t]+#+[ \t]*)?$")


def normalize_plan_path(project_root: Path, plan_path: str | Path) -> Path:
    """Resolve a project-contained regular plan file and reject symlink traversal."""
    root = project_root.resolve(strict=True)
    candidate = Path(plan_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.abspath(candidate))
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise ReviewEvidenceError(
            "invalid_plan_path",
            f"plan path escapes project root: {plan_path}",
        ) from exc
    current = root
    for component in lexical.relative_to(root).parts:
        current /= component
        if current.is_symlink():
            raise ReviewEvidenceError(
                "invalid_plan_path",
                f"symlinked plan paths are forbidden: {plan_path}",
            )
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ReviewEvidenceError(
            "invalid_plan_path",
            f"plan path cannot be resolved: {plan_path}",
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReviewEvidenceError(
            "invalid_plan_path",
            f"plan path escapes project root: {plan_path}",
        ) from exc
    if not resolved.is_file():
        raise ReviewEvidenceError(
            "invalid_plan_path",
            f"plan path is not a regular file: {plan_path}",
        )
    return resolved


def build_section_manifest(snapshot: bytes) -> tuple[SectionHash, ...]:
    """Hash a total, fence-aware partition of level-2..6 markdown sections."""
    try:
        text = snapshot.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewEvidenceError(
            "invalid_plan_encoding",
            f"plan must be valid UTF-8: {exc}",
        ) from exc
    logical_lines = text.splitlines()
    line_chunks = text.splitlines(keepends=True)
    if len(line_chunks) < len(logical_lines):
        line_chunks.extend("" for _ in range(len(logical_lines) - len(line_chunks)))
    offsets = [0]
    for chunk in line_chunks:
        offsets.append(offsets[-1] + len(chunk))
    mask, unclosed_fence_line = compute_fence_mask(logical_lines)
    if unclosed_fence_line is not None:
        raise ReviewEvidenceError(
            "invalid_plan_fence",
            f"unclosed fence opened at line {unclosed_fence_line}",
        )
    headings: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index, line in enumerate(logical_lines):
        if mask[index]:
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        key = manifest_key(line)
        if key in seen:
            raise ReviewEvidenceError(
                "duplicate_manifest_key",
                f"duplicate manifest key: {key}",
                details={"manifest_key": key},
            )
        seen.add(key)
        headings.append((index, key))
    first_offset = offsets[headings[0][0]] if headings else len(text)
    sections = [
        SectionHash(
            section_id=PREAMBLE_SECTION_ID,
            section_hash=_sha256(text[:first_offset].encode("utf-8")),
        )
    ]
    for position, (line_index, key) in enumerate(headings):
        start = offsets[line_index]
        end = offsets[headings[position + 1][0]] if position + 1 < len(headings) else len(text)
        sections.append(
            SectionHash(
                section_id=key,
                section_hash=_sha256(text[start:end].encode("utf-8")),
            )
        )
    return tuple(sections)


def manifest_key(heading: str) -> str:
    """Return the unique manifest identity emitted for one markdown heading."""
    canonical = PLAN_HEADING_REGEX.match(heading)
    if canonical is not None:
        return str(canonical.group("section_id"))
    match = _HEADING_RE.match(heading)
    if match is None:
        raise ReviewEvidenceError("invalid_heading", f"not a level-2..6 heading: {heading}")
    title = " ".join(match.group("title").strip().split())
    if not title:
        raise ReviewEvidenceError("invalid_heading", "manifest heading title cannot be empty")
    if title == PREAMBLE_SECTION_ID:
        raise ReviewEvidenceError(
            "reserved_manifest_key",
            f"heading cannot use reserved manifest key: {PREAMBLE_SECTION_ID}",
        )
    return title


def reviewed_section_hashes(
    sections: Sequence[SectionHash],
) -> dict[str, str]:
    owned = set(COORDINATOR_OWNED_SECTIONS)
    return {
        section.section_id: section.section_hash
        for section in sections
        if section.section_id not in owned
    }


def render_checkpoint(
    *,
    evidence_id: str,
    round_number: int,
    plan_hash: str,
    session_id: str,
    round_result: Mapping[str, object],
) -> bytes:
    payload = {
        "evidence_id": evidence_id,
        "round_number": round_number,
        "plan_hash": plan_hash,
        "session_id": session_id,
        "round_result": validate_round_result(round_result),
    }
    body = canonical_json_bytes(payload).decode("utf-8")
    return f"{CHECKPOINT_FENCE}\n{body}\n```\n".encode()


def parse_checkpoints(plan_bytes: bytes) -> tuple[dict[str, object], ...]:
    try:
        text = plan_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewEvidenceError(
            "checkpoint_reconciliation_error",
            f"plan must be valid UTF-8: {exc}",
        ) from exc
    lines = text.splitlines()
    results: list[dict[str, object]] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != CHECKPOINT_FENCE:
            index += 1
            continue
        if index + 2 >= len(lines) or lines[index + 2].strip() != "```":
            raise ReviewEvidenceError(
                "checkpoint_reconciliation_error",
                f"malformed V1 round checkpoint at line {index + 1}",
            )
        try:
            raw = json.loads(lines[index + 1])
        except json.JSONDecodeError as exc:
            raise ReviewEvidenceError(
                "checkpoint_reconciliation_error",
                f"invalid V1 round checkpoint JSON at line {index + 2}: {exc}",
            ) from exc
        if not isinstance(raw, dict):
            raise ReviewEvidenceError(
                "checkpoint_reconciliation_error",
                f"V1 round checkpoint at line {index + 2} must be an object",
            )
        payload = cast(dict[str, object], raw)
        _validate_checkpoint_envelope(payload)
        results.append(payload)
        index += 3
    return tuple(results)


def ensure_checkpoint(plan_path: Path, checkpoint: bytes) -> bool:
    """Persist a checkpoint once inside the coordinator-owned V1 section."""
    current = plan_path.read_bytes()
    if _checkpoint_is_replayed(current, checkpoint):
        return False
    text = current.decode("utf-8")
    start, end = _section_span(text, "V1")
    addition = checkpoint.decode("utf-8")
    updated_section = text[start:end].rstrip() + "\n\n" + addition
    updated = text[:start] + updated_section + text[end:]
    atomic_write_bytes(plan_path, updated.encode("utf-8"))
    return True


def append_round_entry(plan_path: Path, prose: str, checkpoint: bytes) -> bool:
    """Atomically append one round entry (prose + checkpoint fence) to the V1 section.

    The updated document is validated before any byte reaches the plan file, so
    a validation failure leaves the plan untouched.
    """
    entry_prose = prose.strip()
    if not entry_prose:
        raise ReviewEvidenceError(
            "invalid_round_entry",
            "round entry prose cannot be empty",
        )
    current = plan_path.read_bytes()
    if _checkpoint_is_replayed(current, checkpoint):
        return False
    text = current.decode("utf-8")
    start, end = _section_span(text, "V1")
    addition = f"{entry_prose}\n\n{checkpoint.decode('utf-8')}"
    updated_section = text[start:end].rstrip() + "\n\n" + addition
    tail = text[end:]
    updated = (text[:start] + updated_section + ("\n" + tail if tail else "")).encode("utf-8")
    _validate_round_entry_plan(plan_path, current, updated)
    parsed_checkpoint = parse_checkpoints(checkpoint)
    if len(parsed_checkpoint) != 1:
        raise ReviewEvidenceError(
            "invalid_round_entry",
            "round entry checkpoint must contain exactly one canonical checkpoint",
        )
    expected = parse_checkpoints(current) + parsed_checkpoint
    if parse_checkpoints(updated) != expected:
        raise ReviewEvidenceError(
            "invalid_round_entry",
            "round entry checkpoint list must equal current checkpoints plus the "
            "supplied canonical checkpoint",
        )
    atomic_write_bytes(plan_path, updated)
    return True


def _checkpoint_is_replayed(current: bytes, checkpoint: bytes) -> bool:
    """Return True when this exact checkpoint is already durable in the plan."""
    checkpoint_payload = parse_checkpoints(checkpoint)[0]
    evidence_id = checkpoint_payload["evidence_id"]
    for existing in parse_checkpoints(current):
        if existing["evidence_id"] != evidence_id:
            continue
        if canonical_json_bytes(existing) != canonical_json_bytes(checkpoint_payload):
            raise ReviewEvidenceError(
                "checkpoint_reconciliation_error",
                f"conflicting V1 checkpoint for evidence {evidence_id}",
            )
        return True
    return False


def _validate_round_entry_plan(plan_path: Path, current: bytes, updated: bytes) -> None:
    """Prove the appended entry changed only the V1 section and still parses."""
    current_text = current.decode("utf-8")
    updated_text = updated.decode("utf-8")
    start, end = _section_span(current_text, "V1")
    prefix = current_text[:start]
    suffix = current_text[end:]
    if (
        not updated_text.startswith(prefix)
        or not updated_text.endswith(suffix)
        or len(updated_text) < start + len(suffix)
    ):
        raise ReviewEvidenceError(
            "invalid_round_entry",
            "round entry must modify only the V1 Plan Changelog section",
        )
    try:
        parse_plan_bytes(plan_path.name, updated)
    except ReviewEvidenceError as exc:
        raise ReviewEvidenceError(
            "invalid_round_entry",
            f"plan does not parse after round entry: {exc}",
        ) from exc


def parse_plan_bytes(
    plan_name: str,
    content: bytes,
    *,
    parse_mode: ParseMode = "draft",
) -> PlanDocument:
    """Parse plan bytes under ``plan_name`` without touching the real plan file."""
    try:
        with TemporaryDirectory(prefix="gobby-plan-review-") as temp_dir:
            temp_path = Path(temp_dir) / plan_name
            temp_path.write_bytes(content)
            return parse_plan(temp_path, parse_mode=parse_mode)
    except (OSError, ValueError) as exc:
        raise ReviewEvidenceError("invalid_plan", f"plan does not parse: {exc}") from exc


def render_manifest_plan(
    plan_path: Path,
    plan_bytes: bytes,
    entries: list[dict[str, object]],
) -> bytes:
    """Render and parse a replacement M1 section before touching the live plan."""
    text = plan_bytes.decode("utf-8")
    try:
        start, end = _section_span(text, "M1")
        body = text[:start].rstrip()
        suffix = text[end:].strip()
        if suffix:
            raise ReviewEvidenceError(
                "invalid_manifest",
                "M1 Task Manifest must be the final plan section",
            )
    except ReviewEvidenceError as exc:
        if exc.code != "missing_plan_section":
            raise
        body = text.rstrip()
    yaml_block = yaml.safe_dump(entries, sort_keys=False, default_flow_style=False).rstrip()
    updated = (
        f"{body}\n\n## M1 Task Manifest\n`kind: manifest`\n\n```yaml\n{yaml_block}\n```\n".encode()
    )
    _parse_rendered_plan(plan_path, updated)
    return updated


def atomic_write_bytes(path: Path, content: bytes) -> None:
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _parse_rendered_plan(plan_path: Path, content: bytes) -> None:
    original = parse_plan(plan_path, parse_mode="draft")
    try:
        with TemporaryDirectory(prefix="gobby-plan-review-") as temp_dir:
            temp_path = Path(temp_dir) / plan_path.name
            temp_path.write_bytes(content)
            parse_plan(
                temp_path,
                plan_kind=PlanKind.implementation,
                parse_mode="strict",
                plan_id_override=original.plan_id,
            )
    except ReviewEvidenceError:
        raise
    except (OSError, ValueError) as exc:
        raise ReviewEvidenceError("invalid_manifest", f"manifest does not parse: {exc}") from exc


def _section_span(text: str, wanted_key: str) -> tuple[int, int]:
    logical_lines = text.splitlines()
    chunks = text.splitlines(keepends=True)
    offsets = [0]
    for chunk in chunks:
        offsets.append(offsets[-1] + len(chunk))
    mask, unclosed = compute_fence_mask(logical_lines)
    if unclosed is not None:
        raise ReviewEvidenceError("invalid_plan_fence", f"unclosed fence at line {unclosed}")
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(logical_lines):
        if not mask[index] and _HEADING_RE.match(line):
            headings.append((index, manifest_key(line)))
    matches = [
        position for position, (_line_index, key) in enumerate(headings) if key == wanted_key
    ]
    if len(matches) > 1:
        raise ReviewEvidenceError(
            "duplicate_manifest_key",
            f"duplicate manifest key: {wanted_key}",
        )
    if matches:
        position = matches[0]
        line_index = headings[position][0]
        end_line = headings[position + 1][0] if position + 1 < len(headings) else len(chunks)
        return offsets[line_index], offsets[end_line]
    raise ReviewEvidenceError("missing_plan_section", f"plan has no {wanted_key} section")


def _validate_checkpoint_envelope(payload: Mapping[str, object]) -> None:
    evidence_id = payload.get("evidence_id")
    round_number = payload.get("round_number")
    plan_hash = payload.get("plan_hash")
    session_id = payload.get("session_id")
    result = payload.get("round_result")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise ReviewEvidenceError(
            "checkpoint_reconciliation_error",
            "checkpoint evidence_id must be a non-empty string",
        )
    if not isinstance(round_number, int) or isinstance(round_number, bool) or round_number <= 0:
        raise ReviewEvidenceError(
            "checkpoint_reconciliation_error",
            "checkpoint round_number must be a positive integer",
        )
    if not isinstance(plan_hash, str) or not plan_hash:
        raise ReviewEvidenceError(
            "checkpoint_reconciliation_error",
            "checkpoint plan_hash must be a non-empty string",
        )
    if not isinstance(session_id, str) or not session_id:
        raise ReviewEvidenceError(
            "checkpoint_reconciliation_error",
            "checkpoint session_id must be a non-empty string",
        )
    if not isinstance(result, dict):
        raise ReviewEvidenceError(
            "checkpoint_reconciliation_error",
            "checkpoint round_result must be an object",
        )
    # Durable checkpoints were fully validated when written. Keep this parser
    # structural so later schema changes cannot strand existing canonical plans.


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
