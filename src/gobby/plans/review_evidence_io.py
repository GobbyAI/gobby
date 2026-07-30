"""Plan-file hashing and checkpoint IO for review evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import cast

import yaml

from gobby.plans.parser import (
    PLAN_HEADING_REGEX,
    PlanDocument,
    PlanKind,
    compute_fence_mask,
    parse_plan,
)
from gobby.plans.review_evidence_models import (
    ReviewEvidenceError,
    SectionHash,
    canonical_json_bytes,
    canonical_json_object,
    validate_round_result,
)
from gobby.plans.semantic_lint import collect_target_inventory

PREAMBLE_SECTION_ID = "__preamble__"
DEFAULT_SNAPSHOT_PAGE_BYTES = 8_000
MAX_SNAPSHOT_PAGE_BYTES = 12_000
MAX_SNAPSHOT_PAGE_RESPONSE_CHARS = 12_000
COORDINATOR_OWNED_SECTIONS = ("Task Mapping", "M1", "V1")
CHECKPOINT_FENCE = "```json plan-review-round"
_HEADING_RE = re.compile(r"^(?P<marks>#{2,6})[ \t]+(?P<title>.*?)(?:[ \t]+#+[ \t]*)?$")


@dataclass(frozen=True)
class InterRoundDiff:
    """Causal plan surfaces changed between consecutive snapshots."""

    acceptance_item_ids: tuple[str, ...]
    section_targets: tuple[str, ...]
    symbols: tuple[str, ...]
    contracts: tuple[str, ...]
    targets_by_section: Mapping[str, tuple[str, ...]]
    symbols_by_section: Mapping[str, tuple[str, ...]]
    contracts_by_section: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class SnapshotEnvelope:
    """Canonical immutable byte stream and its paging metadata."""

    content: bytes
    snapshot_hash: str
    total_sections: int
    total_records: int
    bundle_digest: str


def build_inter_round_diff(prior_snapshot: bytes, current_snapshot: bytes) -> InterRoundDiff:
    """Return changed acceptance identities and target files from two snapshots."""
    prior = _parse_snapshot(prior_snapshot)
    current = _parse_snapshot(current_snapshot)
    prior_items = _acceptance_items(prior)
    current_items = _acceptance_items(current)
    changed_item_ids = tuple(
        sorted(
            item_id
            for item_id in set(prior_items) | set(current_items)
            if prior_items.get(item_id) != current_items.get(item_id)
        )
    )
    prior_targets = _section_targets(prior)
    current_targets = _section_targets(current)
    changed_targets: set[str] = set()
    targets_by_section: dict[str, tuple[str, ...]] = {}
    for section_id in sorted(set(prior_targets) | set(current_targets)):
        before = prior_targets.get(section_id, frozenset())
        after = current_targets.get(section_id, frozenset())
        if before != after:
            section_targets = tuple(sorted(before | after))
            targets_by_section[section_id] = section_targets
            changed_targets.update(section_targets)
    symbol_sets_by_section: dict[str, set[str]] = {}
    for item_id in changed_item_ids:
        for item in (prior_items.get(item_id), current_items.get(item_id)):
            if item is None:
                continue
            section_id, _prose, artifact_kind, artifact_ref = item
            if artifact_kind == "symbol":
                symbol_sets_by_section.setdefault(section_id, set()).add(artifact_ref)
    symbols_by_section = {
        section_id: tuple(sorted(symbol_refs))
        for section_id, symbol_refs in sorted(symbol_sets_by_section.items())
    }
    symbols = {
        symbol_ref for symbol_refs in symbols_by_section.values() for symbol_ref in symbol_refs
    }
    contract_sets_by_section: dict[str, set[str]] = {}
    for item_id in changed_item_ids:
        for item in (prior_items.get(item_id), current_items.get(item_id)):
            if item is None:
                continue
            section_id, _prose, artifact_kind, artifact_ref = item
            if artifact_kind == "behavior" or Path(artifact_ref).suffix.lower() in {
                ".json",
                ".md",
                ".sql",
                ".yaml",
                ".yml",
            }:
                contract_sets_by_section.setdefault(section_id, set()).add(artifact_ref)
    for section_id, section_targets in targets_by_section.items():
        contract_sets_by_section.setdefault(section_id, set()).update(
            target
            for target in section_targets
            if Path(target).suffix.lower() in {".json", ".md", ".sql", ".yaml", ".yml"}
        )
    contracts_by_section = {
        section_id: tuple(sorted(contract_refs))
        for section_id, contract_refs in sorted(contract_sets_by_section.items())
        if contract_refs
    }
    contracts = {
        contract for contract_refs in contracts_by_section.values() for contract in contract_refs
    }
    return InterRoundDiff(
        acceptance_item_ids=changed_item_ids,
        section_targets=tuple(sorted(changed_targets)),
        symbols=tuple(sorted(symbols)),
        contracts=tuple(sorted(contracts)),
        targets_by_section=targets_by_section,
        symbols_by_section=symbols_by_section,
        contracts_by_section=contracts_by_section,
    )


def with_consumer_inventory_context(
    prior_round_context: Mapping[str, object],
    *,
    inventory: Mapping[str, object],
) -> dict[str, object]:
    """Transport the canonical consumer inventory into durable round context."""
    context = dict(prior_round_context)
    context["consumer_site_inventory"] = dict(inventory)
    return context


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
    return tuple(
        SectionHash(section_id=section_id, section_hash=_sha256(content))
        for section_id, content in _split_snapshot_sections(snapshot)
    )


def _split_snapshot_sections(snapshot: bytes) -> tuple[tuple[str, bytes], ...]:
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
    sections = [(PREAMBLE_SECTION_ID, text[:first_offset].encode("utf-8"))]
    for position, (line_index, key) in enumerate(headings):
        start = offsets[line_index]
        end = offsets[headings[position + 1][0]] if position + 1 < len(headings) else len(text)
        sections.append((key, text[start:end].encode("utf-8")))
    return tuple(sections)


def serialize_snapshot_envelope(
    *,
    evidence_id: str,
    plan_hash: str,
    round_number: int,
    snapshot: bytes,
    section_manifest: Sequence[SectionHash],
    changed_section_ids: Sequence[str],
    prior_round_context: Mapping[str, object] | None,
    quality_ledger: Sequence[Mapping[str, object]],
    review_complexity: Mapping[str, object],
) -> SnapshotEnvelope:
    """Serialize every immutable review input into one canonical record stream."""
    section_fragments = _split_snapshot_sections(snapshot)
    derived_manifest = tuple(
        SectionHash(section_id=section_id, section_hash=_sha256(content))
        for section_id, content in section_fragments
    )
    if derived_manifest != tuple(section_manifest):
        raise ReviewEvidenceError(
            "section_manifest_mismatch",
            "section manifest differs from the immutable snapshot",
        )

    records: list[dict[str, object]] = [
        {
            "record_type": "plan_section",
            "record_id": section_id,
            "sha256": _sha256(content),
            "content": content.decode("utf-8"),
        }
        for section_id, content in section_fragments
    ]
    context = canonical_json_object(prior_round_context) if prior_round_context is not None else {}
    raw_inventory = context.get("consumer_site_inventory")
    if isinstance(raw_inventory, Mapping):
        context.pop("consumer_site_inventory")
        records.append(
            _json_snapshot_record(
                "consumer_inventory",
                "consumer_site_inventory",
                raw_inventory,
            )
        )
    if context:
        records.append(
            _json_snapshot_record(
                "prior_round_context",
                "prior_round_context",
                context,
            )
        )
    for index, raw_entry in enumerate(quality_ledger):
        entry = canonical_json_object(raw_entry)
        entry_name = entry.get("ledger_id") or entry.get("finding_id") or str(index)
        records.append(
            _json_snapshot_record(
                "quality_ledger_entry",
                f"quality_ledger_entry:{index}:{entry_name}",
                entry,
            )
        )

    descriptors = [
        {
            "record_type": record["record_type"],
            "record_id": record["record_id"],
            "sha256": record["sha256"],
        }
        for record in records
    ]
    bundle_digest = _sha256(canonical_json_bytes({"records": descriptors}))
    payload: dict[str, object] = {
        "version": 1,
        "evidence_id": evidence_id,
        "plan_hash": plan_hash,
        "round_number": round_number,
        "changed_section_ids": sorted(set(changed_section_ids)),
        "review_complexity": canonical_json_object(review_complexity),
        "total_sections": len(section_fragments),
        "total_records": len(records),
        "bundle_digest": bundle_digest,
        "records": records,
    }
    content = canonical_json_bytes(payload)
    return SnapshotEnvelope(
        content=content,
        snapshot_hash=_sha256(content),
        total_sections=len(section_fragments),
        total_records=len(records),
        bundle_digest=bundle_digest,
    )


def paginate_snapshot_envelope(
    envelope: SnapshotEnvelope,
    *,
    offset: int = 0,
    limit: int = DEFAULT_SNAPSHOT_PAGE_BYTES,
) -> dict[str, object]:
    """Return one UTF-8-aligned page whose complete JSON result stays bounded."""
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ReviewEvidenceError(
            "invalid_snapshot_offset",
            "offset must be a non-negative integer",
        )
    if offset > len(envelope.content):
        raise ReviewEvidenceError(
            "invalid_snapshot_offset",
            "offset exceeds the serialized snapshot length",
        )
    if offset < len(envelope.content) and envelope.content[offset] & 0xC0 == 0x80:
        raise ReviewEvidenceError(
            "invalid_snapshot_offset",
            "offset must align to a UTF-8 code-point boundary",
        )
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
        or limit > MAX_SNAPSHOT_PAGE_BYTES
    ):
        raise ReviewEvidenceError(
            "invalid_snapshot_limit",
            f"limit must be between 1 and {MAX_SNAPSHOT_PAGE_BYTES}",
        )

    end = _utf8_boundary_at_or_before(
        envelope.content,
        min(offset + limit, len(envelope.content)),
        floor=offset,
    )
    if end == offset and offset < len(envelope.content):
        raise ReviewEvidenceError(
            "invalid_snapshot_limit",
            "limit is too small for the next UTF-8 code point",
        )
    while True:
        payload = _snapshot_page_payload(envelope, offset=offset, end=end)
        serialized_chars = len(
            json.dumps(
                {"ok": True, **payload},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
        if serialized_chars < MAX_SNAPSHOT_PAGE_RESPONSE_CHARS:
            return payload
        span = end - offset
        next_span = max(1, (span * MAX_SNAPSHOT_PAGE_RESPONSE_CHARS) // serialized_chars - 1)
        next_end = _utf8_boundary_at_or_before(
            envelope.content,
            offset + next_span,
            floor=offset,
        )
        if next_end <= offset:
            raise ReviewEvidenceError(
                "snapshot_page_overflow",
                "one UTF-8 code point exceeds the serialized page budget",
            )
        end = next_end


def parse_snapshot_envelope(
    serialized: bytes,
    *,
    snapshot_hash: str,
) -> dict[str, object]:
    """Verify and reconstruct a complete canonical snapshot envelope locally."""
    if _sha256(serialized) != snapshot_hash:
        raise ReviewEvidenceError(
            "snapshot_hash_mismatch",
            "reconstructed snapshot hash does not match snapshot_hash",
        )
    try:
        raw_payload = json.loads(serialized.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewEvidenceError(
            "invalid_snapshot_envelope",
            f"snapshot envelope is not canonical UTF-8 JSON: {exc}",
        ) from exc
    if not isinstance(raw_payload, dict):
        raise ReviewEvidenceError(
            "invalid_snapshot_envelope",
            "snapshot envelope must be a JSON object",
        )
    payload = cast(dict[str, object], raw_payload)
    raw_records = payload.get("records")
    if payload.get("version") != 1 or not isinstance(raw_records, list):
        raise ReviewEvidenceError(
            "invalid_snapshot_envelope",
            "snapshot envelope version or records are invalid",
        )
    records = [
        cast(dict[str, object], record) for record in raw_records if isinstance(record, dict)
    ]
    if len(records) != len(raw_records) or payload.get("total_records") != len(records):
        raise ReviewEvidenceError(
            "invalid_snapshot_envelope",
            "snapshot envelope total_records does not match its records",
        )

    descriptors: list[dict[str, object]] = []
    seen_records: set[tuple[str, str]] = set()
    plan_chunks: list[bytes] = []
    plan_manifest: list[dict[str, str]] = []
    context: dict[str, object] = {}
    quality_ledger: list[dict[str, object]] = []
    for record in records:
        record_type = record.get("record_type")
        record_id = record.get("record_id")
        digest = record.get("sha256")
        if (
            not isinstance(record_type, str)
            or not record_type
            or not isinstance(record_id, str)
            or not record_id
            or not isinstance(digest, str)
            or not digest
        ):
            raise ReviewEvidenceError(
                "invalid_snapshot_record",
                "snapshot record identity and digest must be non-empty strings",
            )
        identity = (record_type, record_id)
        if identity in seen_records:
            raise ReviewEvidenceError(
                "invalid_snapshot_record",
                f"duplicate snapshot record: {record_type}/{record_id}",
            )
        seen_records.add(identity)
        content = record.get("content")
        if record_type == "plan_section":
            if not isinstance(content, str):
                raise ReviewEvidenceError(
                    "invalid_snapshot_record",
                    "plan section content must be a string",
                )
            content_bytes = content.encode("utf-8")
            plan_chunks.append(content_bytes)
            plan_manifest.append({"section_id": record_id, "section_hash": digest})
        else:
            if not isinstance(content, Mapping):
                raise ReviewEvidenceError(
                    "invalid_snapshot_record",
                    f"{record_type} content must be an object",
                )
            canonical_content = canonical_json_object(content)
            content_bytes = canonical_json_bytes(canonical_content)
            if record_type == "prior_round_context":
                context.update(canonical_content)
            elif record_type == "quality_ledger_entry":
                quality_ledger.append(canonical_content)
            elif record_type == "consumer_inventory":
                context["consumer_site_inventory"] = canonical_content
            else:
                raise ReviewEvidenceError(
                    "invalid_snapshot_record",
                    f"unsupported snapshot record type: {record_type}",
                )
        if _sha256(content_bytes) != digest:
            raise ReviewEvidenceError(
                "snapshot_record_hash_mismatch",
                f"snapshot record hash mismatch: {record_type}/{record_id}",
            )
        descriptors.append(
            {
                "record_type": record_type,
                "record_id": record_id,
                "sha256": digest,
            }
        )

    bundle_digest = _sha256(canonical_json_bytes({"records": descriptors}))
    if payload.get("bundle_digest") != bundle_digest:
        raise ReviewEvidenceError(
            "snapshot_bundle_mismatch",
            "snapshot record bundle digest does not match bundle_digest",
        )
    snapshot = b"".join(plan_chunks)
    plan_hash = payload.get("plan_hash")
    if not isinstance(plan_hash, str) or _sha256(snapshot) != plan_hash:
        raise ReviewEvidenceError(
            "snapshot_plan_hash_mismatch",
            "reconstructed plan bytes do not match plan_hash",
        )
    derived_manifest = [section.to_dict() for section in build_section_manifest(snapshot)]
    if plan_manifest != derived_manifest or payload.get("total_sections") != len(plan_manifest):
        raise ReviewEvidenceError(
            "snapshot_section_union_mismatch",
            "plan section records do not equal the canonical section manifest",
        )
    return {
        **payload,
        "records": records,
        "snapshot": snapshot,
        "section_manifest": plan_manifest,
        "prior_round_context": context or None,
        "quality_ledger": quality_ledger,
    }


def _json_snapshot_record(
    record_type: str,
    record_id: str,
    content: Mapping[str, object],
) -> dict[str, object]:
    canonical_content = canonical_json_object(content)
    return {
        "record_type": record_type,
        "record_id": record_id,
        "sha256": _sha256(canonical_json_bytes(canonical_content)),
        "content": canonical_content,
    }


def _utf8_boundary_at_or_before(content: bytes, end: int, *, floor: int) -> int:
    while end > floor and end < len(content) and content[end] & 0xC0 == 0x80:
        end -= 1
    return end


def _snapshot_page_payload(
    envelope: SnapshotEnvelope,
    *,
    offset: int,
    end: int,
) -> dict[str, object]:
    return {
        "offset": offset,
        "content": envelope.content[offset:end].decode("utf-8"),
        "snapshot_hash": envelope.snapshot_hash,
        "total_bytes": len(envelope.content),
        "total_sections": envelope.total_sections,
        "total_records": envelope.total_records,
        "bundle_digest": envelope.bundle_digest,
        "next_offset": end if end < len(envelope.content) else None,
    }


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
        return False
    text = current.decode("utf-8")
    start, end = _section_span(text, "V1")
    addition = checkpoint.decode("utf-8")
    updated_section = text[start:end].rstrip() + "\n\n" + addition
    updated = text[:start] + updated_section + text[end:]
    atomic_write_bytes(plan_path, updated.encode("utf-8"))
    return True


def render_manifest_plan(
    plan_path: Path,
    plan_bytes: bytes,
    entries: list[dict[str, object]],
) -> bytes:
    """Render and parse a replacement M1 section before touching the live plan."""
    text = plan_bytes.decode("utf-8")
    try:
        start, end = _section_span(text, "M1")
    except ReviewEvidenceError as exc:
        if exc.code != "missing_plan_section":
            raise
        body = text.rstrip()
    else:
        body = text[:start].rstrip()
        suffix = text[end:].strip()
        if suffix:
            raise ReviewEvidenceError(
                "invalid_manifest",
                "M1 Task Manifest must be the final plan section",
            )
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


def _parse_snapshot(snapshot: bytes) -> PlanDocument:
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(snapshot)
        return parse_plan(temp_path, parse_mode="draft")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _acceptance_items(document: PlanDocument) -> dict[str, tuple[str, str, str, str]]:
    return {
        item.item_id: (
            section.section_id,
            item.prose,
            item.artifact_kind.value,
            item.artifact_ref,
        )
        for section in document.sections
        for item in section.acceptance_items
    }


def _section_targets(document: PlanDocument) -> dict[str, frozenset[str]]:
    return {
        section.section_id: collect_target_inventory(document, section)
        for section in document.sections
    }


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
    # Deliberately structural only. This parser reads durable records the system
    # already wrote and accepted; re-validating them against the *current*
    # round-result schema rejects every checkpoint written before that schema
    # gained a required field, which permanently blocks preparation of any plan
    # carrying prior rounds. The full schema is enforced wherever a round result
    # is produced -- render_checkpoint above, terminal delivery, and evidence
    # finalization -- so no write path can create an invalid one.


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
