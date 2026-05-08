"""Coverage manifest pathing and identity protection."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

_ALLOWLIST_RE = re.compile(r"[^A-Za-z0-9._-]")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

type ComponentKind = Literal["project_id", "root_task_ref", "plan_id", "component"]


@dataclass(frozen=True)
class ManifestIdentity:
    project_id: str
    root_task_ref: str
    plan_id: str


class EmptyComponentError(ValueError):
    """Raised when a path component is empty after sanitization."""


class IdentityCollisionError(ValueError):
    """Raised when the same manifest identity is written with a new plan hash."""

    def __init__(self, existing_hash: str, new_hash: str) -> None:
        self.existing_hash = existing_hash
        self.new_hash = new_hash
        super().__init__(
            f"coverage manifest identity already exists with plan hash {existing_hash!r}; "
            f"new hash is {new_hash!r}"
        )


class PathIdentityMismatchError(ValueError):
    """Raised when a manifest path collision maps to a different identity."""


def coverage_manifest_path(
    project_root: Path | str, *, project_id: str, root_task_ref: str, plan_id: str
) -> Path:
    root = Path(project_root)
    return (
        root
        / ".gobby"
        / "plans"
        / "coverage"
        / _sanitize(project_id, kind="project_id")
        / _sanitize(root_task_ref, kind="root_task_ref")
        / f"{_sanitize(plan_id, kind='plan_id')}.coverage.yaml"
    )


def write_manifest(
    report: object,
    project_root: Path | str,
    *,
    regenerate: bool = False,
    manifest_path: Path | str | None = None,
) -> Path:
    identity = ManifestIdentity(
        project_id=_required_header(report, "project_id"),
        root_task_ref=_required_header(report, "root_task_ref"),
        plan_id=_required_header(report, "plan_id"),
    )
    new_hash = _required_header(report, "plan_hash")
    root = Path(project_root)
    path = (
        Path(manifest_path)
        if manifest_path is not None
        else coverage_manifest_path(
            root,
            project_id=identity.project_id,
            root_task_ref=identity.root_task_ref,
            plan_id=identity.plan_id,
        )
    )
    coverage_root = root / ".gobby" / "plans" / "coverage"

    _ensure_path_identity(path, identity, coverage_root)
    existing = _read_manifest(path)
    if existing is not None:
        existing_identity = _identity_from_manifest(existing)
        if existing_identity != identity:
            raise PathIdentityMismatchError(
                f"manifest path {path} already belongs to {existing_identity}"
            )
        existing_hash = _plan_hash_from_manifest(existing)
        if existing_hash != new_hash:
            if not regenerate:
                raise IdentityCollisionError(existing_hash, new_hash)
            _append_regenerate_audit(coverage_root, identity, existing_hash, new_hash)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_manifest_payload(report), sort_keys=False), encoding="utf-8")
    return path


def _sanitize(value: str, *, kind: ComponentKind = "component") -> str:
    raw = value[1:] if kind == "root_task_ref" and value.startswith("#") else value
    replaced = _ALLOWLIST_RE.sub("-", raw).strip("-._")
    if not replaced:
        raise EmptyComponentError(f"{kind} is empty after sanitization")
    if len(replaced) > 64:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:7]
        replaced = f"{replaced[:56]}-{suffix}"
    if replaced.upper() in _WINDOWS_RESERVED:
        replaced = f"{replaced}_"
    return replaced


def _ensure_path_identity(path: Path, identity: ManifestIdentity, coverage_root: Path) -> None:
    for existing_path in _candidate_manifest_paths(path, coverage_root):
        existing = _read_manifest(existing_path)
        existing_identity = _identity_from_manifest(existing) if existing is not None else None
        if existing_identity == identity:
            continue
        if existing_path == path:
            raise PathIdentityMismatchError(
                f"manifest path {path} already belongs to {existing_identity}"
            )
        if _casefold_same_path(existing_path, path) or _casefold_component_collision(
            existing_path, path, coverage_root
        ):
            raise PathIdentityMismatchError(
                f"manifest path {path} collides with {existing_path} for {existing_identity}"
            )


def _candidate_manifest_paths(path: Path, coverage_root: Path) -> tuple[Path, ...]:
    candidates = {path} if path.exists() else set()
    if coverage_root.exists():
        candidates.update(coverage_root.rglob("*.coverage.yaml"))
    if path.parent.exists():
        candidates.update(path.parent.glob("*.coverage.yaml"))
    return tuple(candidates)


def _casefold_same_path(left: Path, right: Path) -> bool:
    return tuple(part.casefold() for part in left.parts) == tuple(
        part.casefold() for part in right.parts
    )


def _casefold_component_collision(existing: Path, target: Path, coverage_root: Path) -> bool:
    existing_parts = _relative_parts(existing, coverage_root)
    target_parts = _relative_parts(target, coverage_root)
    if existing_parts is None or target_parts is None:
        return False
    for index in range(min(2, len(existing_parts), len(target_parts))):
        if (
            existing_parts[index].casefold() == target_parts[index].casefold()
            and existing_parts[index] != target_parts[index]
        ):
            return True
    if len(existing_parts) >= 3 and len(target_parts) >= 3:
        same_parent = tuple(part.casefold() for part in existing_parts[:-1]) == tuple(
            part.casefold() for part in target_parts[:-1]
        )
        same_leaf = existing_parts[-1].casefold() == target_parts[-1].casefold()
        different_leaf = existing_parts[-1] != target_parts[-1]
        return same_parent and same_leaf and different_leaf
    return False


def _relative_parts(path: Path, root: Path) -> tuple[str, ...] | None:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return None


def _read_manifest(path: Path) -> Mapping[str, object] | None:
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, Mapping):
        return raw
    return {}


def _identity_from_manifest(raw: Mapping[str, object]) -> ManifestIdentity | None:
    header = raw.get("header")
    if not isinstance(header, Mapping):
        return None
    project_id = header.get("project_id")
    root_task_ref = header.get("root_task_ref")
    plan_id = header.get("plan_id")
    if not all(isinstance(value, str) and value for value in (project_id, root_task_ref, plan_id)):
        return None
    return ManifestIdentity(
        project_id=str(project_id),
        root_task_ref=str(root_task_ref),
        plan_id=str(plan_id),
    )


def _plan_hash_from_manifest(raw: Mapping[str, object]) -> str:
    header = raw.get("header")
    if not isinstance(header, Mapping):
        return ""
    value = header.get("plan_hash")
    return value if isinstance(value, str) else ""


def _append_regenerate_audit(
    coverage_root: Path,
    identity: ManifestIdentity,
    existing_hash: str,
    new_hash: str,
) -> None:
    coverage_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    line = (
        f"{timestamp} {identity.project_id} {identity.root_task_ref} {identity.plan_id} "
        f"{existing_hash} -> {new_hash}\n"
    )
    with (coverage_root / ".regenerate.log").open("a", encoding="utf-8") as log_file:
        log_file.write(line)


def _required_header(report: object, field: str) -> str:
    header = _attr(report, "header")
    value = _attr(header, field)
    if not isinstance(value, str) or not value:
        raise EmptyComponentError(f"manifest header {field} is required")
    return value


def _manifest_payload(report: object) -> dict[str, object]:
    return {
        "header": _header_payload(_attr(report, "header")),
        "rows": [_row_payload(row) for row in _iter_attr(report, "rows")],
    }


def _header_payload(header: object) -> dict[str, object]:
    return {
        "plan_id": _attr(header, "plan_id"),
        "plan_hash": _attr(header, "plan_hash"),
        "root_task_ref": _attr(header, "root_task_ref"),
        "project_id": _attr(header, "project_id"),
        "generated_at": _attr(header, "generated_at"),
        "task_tree_source": _value(_attr(header, "task_tree_source")),
        "task_tree_source_hash": _attr(header, "task_tree_source_hash"),
        "evidence_summary": _attr(header, "evidence_summary"),
    }


def _row_payload(row: object) -> dict[str, object]:
    return {
        "section_id": _attr(row, "section_id"),
        "item_id": _attr(row, "item_id"),
        "status": _value(_attr(row, "status")),
        "leaves": [_leaf_payload(leaf) for leaf in _iter_attr(row, "leaves")],
        "deferral_target": _attr(row, "deferral_target"),
        "evidence": [_evidence_payload(evidence) for evidence in _iter_attr(row, "evidence")],
    }


def _leaf_payload(leaf: object) -> dict[str, object]:
    return {
        "leaf_task_ref": _attr(leaf, "leaf_task_ref"),
        "validation_criteria_snippet": _attr(leaf, "validation_criteria_snippet"),
        "matched_artifact_ref": _attr(leaf, "matched_artifact_ref"),
    }


def _evidence_payload(evidence: object) -> dict[str, object]:
    return {
        "kind": _value(_attr(evidence, "kind")),
        "ref": _attr(evidence, "ref"),
        "status": _value(_attr(evidence, "status")),
        "detail": _attr(evidence, "detail"),
        "artifacts_touched": list(_iter_attr(evidence, "artifacts_touched")),
    }


def _value(raw: object) -> object:
    return getattr(raw, "value", raw)


def _attr(raw: object, name: str) -> object:
    return getattr(raw, name)


def _iter_attr(raw: object, name: str) -> Iterable[object]:
    value = _attr(raw, name)
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        return value
    return ()


__all__ = [
    "EmptyComponentError",
    "IdentityCollisionError",
    "ManifestIdentity",
    "PathIdentityMismatchError",
    "coverage_manifest_path",
    "write_manifest",
]
