"""Bootstrap ledger revalidation for plan coverage manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.storage.database import DatabaseProtocol


@dataclass(frozen=True)
class _TaskPlanScope:
    project_id: str
    root_task_ref: str
    repo_root: Path


class BootstrapLedgerMismatchError(ValueError):
    """Raised when a committed manifest no longer matches its bootstrap ledger."""

    def __init__(
        self,
        mismatches: Sequence[str],
        *,
        plan_id: str | None = None,
        ledger_path: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self.mismatches = tuple(mismatches)
        self.plan_id = plan_id
        self.ledger_path = str(ledger_path) if ledger_path is not None else None
        self.manifest_path = str(manifest_path) if manifest_path is not None else None
        preview = "; ".join(self.mismatches[:3])
        if len(self.mismatches) > 3:
            preview += f"; and {len(self.mismatches) - 3} more"
        super().__init__(preview or "bootstrap ledger mismatch")

    def to_response(self) -> dict[str, object]:
        """Return a structured MCP-safe error payload."""
        return {
            "success": False,
            "error": "bootstrap_ledger_mismatch",
            "message": str(self),
            "plan_id": self.plan_id,
            "ledger_path": self.ledger_path,
            "manifest_path": self.manifest_path,
            "mismatches": list(self.mismatches),
        }


def bootstrap_ledger_path_for_task(db: DatabaseProtocol, task_id: str) -> Path | None:
    """Return the companion bootstrap ledger path for a root plan task, if one exists."""
    scope = _scope_for_task(db, task_id)
    if scope is None:
        return None

    for entry in _matching_plan_entries(db, scope):
        plan_id = _required_string(entry, "plan_id")
        if plan_id is None:
            continue
        ledger_path = scope.repo_root / ".gobby" / "plans" / f"{plan_id}.coverage-ledger.yaml"
        if ledger_path.is_file():
            return ledger_path
    return None


def verify_bootstrap_ledger(db: DatabaseProtocol, task_id: str) -> None:
    """Verify any companion bootstrap ledger for the root task against its manifest."""
    scope = _scope_for_task(db, task_id)
    if scope is None:
        return

    mismatches: list[str] = []
    last_plan_id: str | None = None
    last_ledger_path: Path | None = None
    last_manifest_path: Path | None = None

    for entry in _matching_plan_entries(db, scope):
        plan_id = _required_string(entry, "plan_id")
        if plan_id is None:
            continue
        ledger_path = scope.repo_root / ".gobby" / "plans" / f"{plan_id}.coverage-ledger.yaml"
        if not ledger_path.is_file():
            continue
        last_plan_id = plan_id
        last_ledger_path = ledger_path
        plan_mismatches, manifest_path = _verify_one_ledger(
            db,
            scope=scope,
            entry=entry,
            ledger_path=ledger_path,
        )
        last_manifest_path = manifest_path
        mismatches.extend(plan_mismatches)

    if mismatches:
        raise BootstrapLedgerMismatchError(
            mismatches,
            plan_id=last_plan_id,
            ledger_path=last_ledger_path,
            manifest_path=last_manifest_path,
        )


def _verify_one_ledger(
    db: DatabaseProtocol,
    *,
    scope: _TaskPlanScope,
    entry: Mapping[str, object],
    ledger_path: Path,
) -> tuple[list[str], Path | None]:
    mismatches: list[str] = []
    ledger = _load_yaml_mapping(ledger_path)
    if ledger is None:
        return ([f"{ledger_path} is not a YAML mapping"], None)

    plan_id = _required_string(ledger, "plan_id") or _required_string(entry, "plan_id")
    project_id = _required_string(ledger, "project_id") or scope.project_id
    root_task_ref = _required_string(ledger, "root_task_ref") or _required_string(
        entry, "root_task_ref"
    )
    plan_hash = _required_string(ledger, "plan_hash")

    for field_name, value in (
        ("plan_id", plan_id),
        ("project_id", project_id),
        ("root_task_ref", root_task_ref),
        ("plan_hash", plan_hash),
    ):
        if not value:
            mismatches.append(f"{ledger_path} missing {field_name}")

    if not plan_id or not project_id or not root_task_ref:
        return (mismatches, None)

    manifest_path = coverage_manifest_path(
        scope.repo_root,
        project_id=project_id,
        root_task_ref=root_task_ref,
        plan_id=plan_id,
    )
    manifest = _load_yaml_mapping(manifest_path)
    if manifest is None:
        mismatches.append(f"manifest missing or invalid: {manifest_path}")
        return (mismatches, manifest_path)

    mismatches.extend(
        _header_mismatches(
            manifest,
            plan_id=plan_id,
            project_id=project_id,
            root_task_ref=root_task_ref,
            plan_hash=plan_hash,
        )
    )
    mismatches.extend(
        _leaf_mismatches(
            db,
            ledger,
            manifest,
            project_id=project_id,
        )
    )
    return (mismatches, manifest_path)


def _scope_for_task(db: DatabaseProtocol, task_id: str) -> _TaskPlanScope | None:
    row = db.fetchone(
        """
        SELECT t.project_id, t.seq_num, p.repo_path
        FROM tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.id = ?
        """,
        (task_id,),
    )
    if row is None or not row["project_id"] or not row["repo_path"]:
        return None
    root_task_ref = str(row["seq_num"]) if row["seq_num"] is not None else task_id
    return _TaskPlanScope(
        project_id=str(row["project_id"]),
        root_task_ref=root_task_ref,
        repo_root=Path(str(row["repo_path"])),
    )


def _matching_plan_entries(
    db: DatabaseProtocol,
    scope: _TaskPlanScope,
) -> tuple[Mapping[str, object], ...]:
    rows = db.fetchall(
        """
        SELECT plan_id, project_id, root_task_ref
        FROM plans
        WHERE project_id = ?
          AND (root_task_ref = ? OR root_task_ref = ?)
        ORDER BY updated_at DESC, plan_id ASC
        """,
        (scope.project_id, scope.root_task_ref, _normalize_ref(scope.root_task_ref)),
    )
    return tuple(dict(row) for row in rows)


def _header_mismatches(
    manifest: Mapping[str, object],
    *,
    plan_id: str,
    project_id: str,
    root_task_ref: str,
    plan_hash: str | None,
) -> list[str]:
    header = manifest.get("header")
    if not isinstance(header, Mapping):
        return ["manifest header missing"]

    mismatches: list[str] = []
    expected = {
        "plan_id": plan_id,
        "project_id": project_id,
        "root_task_ref": root_task_ref,
    }
    if plan_hash:
        expected["plan_hash"] = plan_hash

    for field_name, expected_value in expected.items():
        actual_value = _required_string(header, field_name)
        if field_name == "root_task_ref":
            if _normalize_ref(actual_value) != _normalize_ref(expected_value):
                mismatches.append(
                    f"manifest header root_task_ref {actual_value!r} != {expected_value!r}"
                )
        elif actual_value != expected_value:
            mismatches.append(
                f"manifest header {field_name} {actual_value!r} != {expected_value!r}"
            )
    return mismatches


def _leaf_mismatches(
    db: DatabaseProtocol,
    ledger: Mapping[str, object],
    manifest: Mapping[str, object],
    *,
    project_id: str,
) -> list[str]:
    expected = _expected_leaf_titles(ledger)
    actual = _manifest_leaf_titles(db, manifest, project_id=project_id)
    mismatches: list[str] = []

    for key, expected_titles in expected.items():
        actual_titles = actual.get(key, ())
        if set(actual_titles) != set(expected_titles):
            section_id, item_id = key
            mismatches.append(
                f"{section_id}:{item_id} expected leaves {sorted(expected_titles)!r}, "
                f"manifest has {sorted(actual_titles)!r}"
            )

    for key, actual_titles in actual.items():
        if key not in expected and actual_titles:
            section_id, item_id = key
            mismatches.append(
                f"{section_id}:{item_id} has unexpected manifest leaves {sorted(actual_titles)!r}"
            )

    return mismatches


def _expected_leaf_titles(ledger: Mapping[str, object]) -> dict[tuple[str, str], tuple[str, ...]]:
    sections = ledger.get("sections")
    if not isinstance(sections, Mapping):
        return {}

    expected: dict[tuple[str, str], tuple[str, ...]] = {}
    for section_id, raw_section in sections.items():
        if not isinstance(raw_section, Mapping):
            continue
        items = raw_section.get("acceptance_items")
        if not isinstance(items, Mapping):
            continue
        for item_id, raw_item in items.items():
            if not isinstance(raw_item, Mapping):
                continue
            titles = []
            for raw_leaf in _sequence(raw_item.get("expected_leaves")):
                if isinstance(raw_leaf, Mapping):
                    title = _required_string(raw_leaf, "title")
                    if title:
                        titles.append(title)
            expected[(str(section_id), str(item_id))] = tuple(titles)
    return expected


def _manifest_leaf_titles(
    db: DatabaseProtocol,
    manifest: Mapping[str, object],
    *,
    project_id: str,
) -> dict[tuple[str, str], tuple[str, ...]]:
    titles_by_item: dict[tuple[str, str], tuple[str, ...]] = {}
    for raw_row in _sequence(manifest.get("rows")):
        if not isinstance(raw_row, Mapping):
            continue
        key = (
            str(raw_row.get("section_id", "")),
            str(raw_row.get("item_id", "")),
        )
        titles: list[str] = []
        for raw_leaf in _sequence(raw_row.get("leaves")):
            if not isinstance(raw_leaf, Mapping):
                continue
            leaf_ref = _required_string(raw_leaf, "leaf_task_ref")
            if not leaf_ref:
                continue
            titles.append(_task_title(db, project_id=project_id, task_ref=leaf_ref) or leaf_ref)
        titles_by_item[key] = tuple(titles)
    return titles_by_item


def _task_title(db: DatabaseProtocol, *, project_id: str, task_ref: str) -> str | None:
    normalized = _normalize_ref(task_ref)
    if normalized.isdigit():
        row = db.fetchone(
            "SELECT title FROM tasks WHERE project_id = ? AND seq_num = ?",
            (project_id, int(normalized)),
        )
        if row is not None:
            return str(row["title"])

    escaped = normalized.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    row = db.fetchone(
        """
        SELECT title FROM tasks
        WHERE project_id = ? AND (id = ? OR id LIKE ? ESCAPE '!')
        ORDER BY id
        LIMIT 1
        """,
        (project_id, normalized, f"{escaped}%"),
    )
    return str(row["title"]) if row is not None else None


def _load_yaml_mapping(path: Path) -> Mapping[str, object] | None:
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, Mapping) else None


def _required_string(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _sequence(raw: object) -> tuple[Any, ...]:
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return tuple(raw)
    return ()


def _normalize_ref(task_ref: str | None) -> str:
    if task_ref is None:
        return ""
    return str(task_ref).removeprefix("#")
