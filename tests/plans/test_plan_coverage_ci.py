from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

import pytest
import yaml

from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.plans.parser import PlanKind, parse_plan

pytestmark = pytest.mark.unit

PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLANS_DIR = PROJECT_ROOT / ".gobby" / "plans"
COVERAGE_DIR = PLANS_DIR / "coverage"
RETIRED_PLAN_STATE_FILES = (
    PLANS_DIR / ("index" + ".yaml"),
    PLANS_DIR / (".grand" + "fathered"),
    PLANS_DIR / (".grand" + "fathered-task-state.yaml"),
    PLANS_DIR / (".legacy" + "-classification.yaml"),
)


@dataclass(frozen=True)
class PlanRegistryEntry:
    plan_id: str
    project_id: str
    root_task_ref: str
    plan_kind: str
    state: str
    plan_path: Path
    plan_hash: str | None


class TestDatabase(Protocol):
    __test__: ClassVar[bool] = False

    def fetchall(self, sql: str) -> list[dict[str, Any]]: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None: ...


def test_retired_plan_state_files_are_removed() -> None:
    offenders = [
        str(path.relative_to(PROJECT_ROOT)) for path in RETIRED_PLAN_STATE_FILES if path.exists()
    ]

    assert not offenders, "retired plan state files still exist: " + ", ".join(offenders)


def test_plan_registry_queries_plans_table(temp_db: TestDatabase) -> None:
    entries = _seed_registry(temp_db)

    rows = temp_db.fetchall("SELECT plan_id, state, plan_kind FROM plans ORDER BY plan_id")

    assert [row["plan_id"] for row in rows] == sorted(entries)
    assert {row["state"] for row in rows} <= {"active", "archived"}
    assert {row["plan_kind"] for row in rows} <= {"implementation", "strategy"}


def test_registered_active_plan_files_parse_with_declared_kind(temp_db: TestDatabase) -> None:
    parsed = []
    for entry in _active_entries(temp_db):
        parsed.append(
            parse_plan(
                entry.plan_path,
                plan_kind=_parser_kind(entry.plan_kind),
                parse_mode="draft",
            )
        )
    assert len(parsed) == len(_active_entries(temp_db))


def test_active_implementation_manifests_match_on_disk(temp_db: TestDatabase) -> None:
    for entry in _active_implementation_entries(temp_db):
        manifest = _read_required_manifest(entry)
        header = _manifest_header(manifest)
        plan_hash = parse_plan(entry.plan_path, parse_mode="draft").source_hash

        assert header.get("plan_id") == entry.plan_id
        assert header.get("project_id") == entry.project_id
        assert header.get("root_task_ref") == entry.root_task_ref
        assert header.get("plan_hash") == plan_hash


def test_zero_missing_invalid_manifest_rows(temp_db: TestDatabase) -> None:
    bad_rows: list[str] = []
    for entry in _active_implementation_entries(temp_db):
        manifest = _read_required_manifest(entry)
        for row in manifest.get("rows", []):
            if isinstance(row, dict) and row.get("status") in {"missing", "invalid"}:
                bad_rows.append(f"{entry.plan_id}:{row.get('section_id')}:{row.get('item_id')}")

    assert not bad_rows, "coverage manifest rows not covered: " + ", ".join(bad_rows)


def test_no_orphan_manifests(temp_db: TestDatabase) -> None:
    _seed_registry(temp_db)
    implementation_keys = {
        (entry.project_id, entry.root_task_ref, entry.plan_id)
        for entry in _active_implementation_entries(temp_db)
    }

    orphaned: list[str] = []
    for manifest_path in _manifest_paths():
        identity = _manifest_identity(manifest_path)
        if identity not in implementation_keys:
            orphaned.append(str(manifest_path.relative_to(PROJECT_ROOT)))
    assert not orphaned, "orphan manifests: " + ", ".join(orphaned)


def _seed_registry(db: TestDatabase) -> dict[str, PlanRegistryEntry]:
    _seed_project(db)
    entries: dict[str, PlanRegistryEntry] = {}
    for path in _discover_plan_files():
        entry = _entry_for_path(path)
        entries[entry.plan_id] = entry
        db.execute(
            """
            INSERT INTO plans (
                id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
                root_task_ref, created_at, updated_at, archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?)
            """,
            (
                f"plan-{entry.plan_id}",
                entry.project_id,
                entry.plan_id,
                str(entry.plan_path.relative_to(PROJECT_ROOT)),
                entry.plan_hash,
                entry.plan_kind,
                entry.state,
                entry.root_task_ref,
                "2026-01-01T00:00:00Z" if entry.state == "archived" else None,
            ),
        )
    return entries


def _seed_project(db: TestDatabase) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO projects (id, name, repo_path, created_at, updated_at)
        VALUES (?, 'gobby', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (PROJECT_ID, str(PROJECT_ROOT)),
    )


def _discover_plan_files() -> list[Path]:
    plan_files = sorted(path for path in PLANS_DIR.glob("*.md") if _is_plan_markdown(path))
    completed_files = sorted(
        path for path in (PLANS_DIR / "completed").glob("*.md") if _is_plan_markdown(path)
    )
    return plan_files + completed_files


def _entry_for_path(path: Path) -> PlanRegistryEntry:
    plan_id = path.stem
    root_task_ref = _root_ref(path)
    state = "archived" if path.parent.name == "completed" else "active"
    manifest_path = coverage_manifest_path(
        PROJECT_ROOT,
        project_id=PROJECT_ID,
        root_task_ref=root_task_ref,
        plan_id=plan_id,
    )
    plan_kind = "implementation" if state == "active" and manifest_path.exists() else "strategy"
    parser_kind = _parser_kind(plan_kind)
    plan_hash = (
        parse_plan(path, plan_kind=parser_kind, parse_mode="draft").source_hash
        if state == "active"
        else None
    )
    return PlanRegistryEntry(
        plan_id=plan_id,
        project_id=PROJECT_ID,
        root_task_ref=root_task_ref,
        plan_kind=plan_kind,
        state=state,
        plan_path=path,
        plan_hash=plan_hash,
    )


def _active_entries(db: TestDatabase) -> list[PlanRegistryEntry]:
    return _entries(db, "SELECT * FROM plans WHERE state = 'active' ORDER BY plan_id")


def _active_implementation_entries(db: TestDatabase) -> list[PlanRegistryEntry]:
    return _entries(
        db,
        """
        SELECT * FROM plans
        WHERE state = 'active' AND plan_kind = 'implementation'
        ORDER BY plan_id
        """,
    )


def _entries(db: TestDatabase, sql: str) -> list[PlanRegistryEntry]:
    rows = db.fetchall(sql)
    return [
        PlanRegistryEntry(
            plan_id=str(row["plan_id"]),
            project_id=str(row["project_id"]),
            root_task_ref=str(row["root_task_ref"]),
            plan_kind=str(row["plan_kind"]),
            state=str(row["state"]),
            plan_path=PROJECT_ROOT / str(row["plan_path"]),
            plan_hash=row["plan_hash"],
        )
        for row in rows
    ]


def _is_plan_markdown(path: Path) -> bool:
    if path.name == "README.md" or path.name.startswith("."):
        return False
    return _strip_leading_html_comments(path.read_text(encoding="utf-8")).lstrip().startswith("#")


def _strip_leading_html_comments(text: str) -> str:
    stripped = text.lstrip()
    while stripped.startswith("<!--"):
        end = stripped.find("-->")
        if end == -1:
            return stripped
        stripped = stripped[end + 3 :].lstrip()
    return stripped


def _root_ref(path: Path) -> str:
    stem = path.stem
    if stem.startswith("task-"):
        token = stem.split("-", 2)[1]
        if token.isdecimal():
            return token
    raise AssertionError(f"cannot infer root task ref from {path}")


def _parser_kind(plan_kind: str) -> PlanKind:
    if plan_kind == "implementation":
        return PlanKind.implementation
    if plan_kind == "strategy":
        return PlanKind.strategy
    raise AssertionError(f"unknown plan_kind {plan_kind!r}")


def _read_required_manifest(entry: PlanRegistryEntry) -> dict[str, Any]:
    manifest_path = coverage_manifest_path(
        PROJECT_ROOT,
        project_id=entry.project_id,
        root_task_ref=entry.root_task_ref,
        plan_id=entry.plan_id,
    )
    assert manifest_path.exists(), f"missing manifest for {entry.plan_id}: {manifest_path}"
    return _load_mapping(manifest_path)


def _manifest_paths() -> list[Path]:
    if not COVERAGE_DIR.exists():
        return []
    return sorted(COVERAGE_DIR.rglob("*.coverage.yaml"))


def _manifest_identity(path: Path) -> tuple[str, str, str]:
    manifest = _load_mapping(path)
    header = _manifest_header(manifest)
    return (
        _required_string(header, "project_id", path=path),
        _required_string(header, "root_task_ref", path=path),
        _required_string(header, "plan_id", path=path),
    )


def _manifest_header(manifest: dict[str, Any]) -> dict[str, Any]:
    header = manifest.get("header")
    assert isinstance(header, dict), "coverage manifest missing header mapping"
    return header


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict), f"{path.relative_to(PROJECT_ROOT)} must be a YAML mapping"
    return raw


def _required_string(raw: dict[str, Any], key: str, *, path: Path) -> str:
    value = raw.get(key)
    assert isinstance(value, str) and value.strip(), (
        f"{path.relative_to(PROJECT_ROOT)} missing non-empty {key}"
    )
    return value


def test_plans_table_has_unique_project_plan_constraint(temp_db: TestDatabase) -> None:
    _seed_registry(temp_db)
    first = _active_entries(temp_db)[0]

    with pytest.raises(sqlite3.IntegrityError):
        temp_db.execute(
            """
            INSERT INTO plans (
                id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
                root_task_ref, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (
                "duplicate-plan-row",
                first.project_id,
                first.plan_id,
                str(first.plan_path.relative_to(PROJECT_ROOT)),
                first.plan_hash,
                first.plan_kind,
                first.state,
                first.root_task_ref,
            ),
        )
