from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.plans.parser import PlanKind, parse_plan

pytestmark = pytest.mark.unit

PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLANS_DIR = PROJECT_ROOT / ".gobby" / "plans"
INDEX_PATH = PLANS_DIR / "index.yaml"
GRANDFATHERED_PATH = PLANS_DIR / ".grandfathered"
GRANDFATHERED_STATE_PATH = PLANS_DIR / ".grandfathered-task-state.yaml"
LEGACY_CLASSIFICATION_PATH = PLANS_DIR / ".legacy-classification.yaml"
COVERAGE_DIR = PLANS_DIR / "coverage"

EXPECTED_PLAN_IDS = {
    "task-12068-skillsmp-install-rewrite",
    "task-12725-lifecycle-dispatch-rev1",
    "task-12746-neo4j-falkordb-swap",
    "task-12761-postgres-hub-migration",
    "task-12898-memory-recall-helper",
}
PLAN_KINDS = {"implementation", "strategy", "legacy"}
STATUSES = {"active", "merged", "archived"}
REMOVE_BY_RE = re.compile(r"#\s*remove-by:\s*(?P<ref>#?\d+)\b")


@dataclass(frozen=True)
class PlanIndexEntry:
    plan_id: str
    project_id: str
    root_task_ref: str
    plan_kind: str
    status: str

    @property
    def plan_path(self) -> Path:
        return PLANS_DIR / f"{self.plan_id}.md"


@dataclass(frozen=True)
class GrandfatheredEntry:
    line_number: int
    raw: str
    remove_by: str | None


@dataclass(frozen=True)
class LiveTask:
    exists: bool
    open: bool
    title: str


def test_index_file_present_and_well_formed() -> None:
    index = _load_index()

    assert set(index) == EXPECTED_PLAN_IDS
    assert len(index) == 5


def test_index_inventory_matches_repo() -> None:
    index = _load_index()

    _assert_index_inventory(index)


def test_every_plan_file_has_index_entry() -> None:
    index = _load_index()
    plan_ids = _discover_plan_ids()

    missing = plan_ids - set(index)
    assert not missing, f"unindexed plan files: {sorted(missing)}"


def test_every_index_entry_has_plan_file() -> None:
    index = _load_index()

    missing = [entry.plan_id for entry in index.values() if not entry.plan_path.exists()]
    assert not missing, f"index entries without plan files: {missing}"


def test_indexed_plan_files_parse_with_declared_kind() -> None:
    for entry in _load_index().values():
        parse_plan(entry.plan_path, plan_kind=_resolve_parser_kind(entry.plan_kind))


def test_parse_plan_dispatch_by_plan_kind() -> None:
    assert _resolve_parser_kind("implementation") is PlanKind.implementation
    assert _resolve_parser_kind("strategy") is PlanKind.strategy
    assert _resolve_parser_kind("legacy") is PlanKind.strategy

    with pytest.raises(AssertionError, match="unknown plan_kind"):
        _resolve_parser_kind("notes")


def test_every_active_implementation_plan_has_manifest() -> None:
    for entry in _implementation_entries().values():
        manifest_path = _coverage_manifest_path(entry)
        assert manifest_path.exists(), (
            f"missing manifest for {entry.plan_id}: expected {manifest_path}"
        )


def test_manifest_plan_hash_matches_on_disk() -> None:
    for entry in _implementation_entries().values():
        manifest = _read_required_manifest(entry)
        header = _manifest_header(manifest)
        plan_hash = parse_plan(entry.plan_path).source_hash

        assert header.get("plan_hash") == plan_hash, (
            f"{entry.plan_id} manifest hash drift: "
            f"manifest={header.get('plan_hash')!r} on_disk={plan_hash!r}"
        )


def test_zero_missing_invalid_rows(tmp_path: Path) -> None:
    for entry in _implementation_entries().values():
        manifest_path = _coverage_manifest_path(entry)
        manifest = _read_required_manifest(entry)
        plan_hash = _manifest_header(manifest)["plan_hash"]
        output_manifest = tmp_path / f"{entry.plan_id}.coverage.yaml"

        result = subprocess.run(
            [
                "uv",
                "run",
                "gobby",
                "plan",
                "coverage",
                "--plan",
                str(entry.plan_path),
                "--plan-id",
                entry.plan_id,
                "--plan-hash",
                str(plan_hash),
                "--task-tree",
                "matrix-file",
                "--matrix-file",
                str(manifest_path),
                "--manifest",
                str(output_manifest),
                "--regenerate",
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"matrix-file revalidation failed for {entry.plan_id}: "
            f"exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def test_no_orphan_manifests() -> None:
    _assert_no_orphan_manifests(_load_index())


def test_strategy_plans_have_no_manifests() -> None:
    _assert_no_manifests_for_plan_kind("strategy")


def test_legacy_plans_have_no_manifests() -> None:
    _assert_no_manifests_for_plan_kind("legacy")


def test_grandfathered_entries_require_remove_by_annotation() -> None:
    missing = [
        f"line {entry.line_number}: {entry.raw}"
        for entry in _grandfathered_entries()
        if entry.remove_by is None
    ]
    assert not missing, ".grandfathered entries missing remove-by annotation: " + ", ".join(missing)


def test_grandfathered_target_task_exists_and_open_via_snapshot() -> None:
    _assert_grandfathered_snapshot()


def test_grandfathered_snapshot_matches_live_db_when_available() -> None:
    if not _live_db_enabled():
        return

    snapshot = _load_grandfathered_snapshot()
    for ref in snapshot["refs"]:
        task_ref = str(ref["task_ref"])
        live = _live_task(task_ref)
        if not live.exists:
            continue
        assert ref["exists"] is live.exists, f"{task_ref} exists snapshot drift"
        assert ref["open"] is live.open, f"{task_ref} open snapshot drift"
        assert ref["title"] == live.title, f"{task_ref} title snapshot drift"


def test_no_unauthorized_grandfathered_additions() -> None:
    diff_base = _grandfathered_diff_base()
    result = subprocess.run(
        [
            "git",
            "diff",
            "--unified=0",
            diff_base,
            "HEAD",
            "--",
            str(GRANDFATHERED_PATH.relative_to(PROJECT_ROOT)),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    unauthorized = [
        line[1:]
        for line in result.stdout.splitlines()
        if line.startswith("+")
        and not line.startswith("+++")
        and line[1:].strip()
        and not line[1:].lstrip().startswith("#")
        and REMOVE_BY_RE.search(line[1:]) is None
    ]
    assert not unauthorized, "new .grandfathered entries missing remove-by: " + ", ".join(
        unauthorized
    )


def _grandfathered_diff_base() -> str:
    candidates: list[str] = []
    base_sha = os.environ.get("GITHUB_BASE_SHA")
    if base_sha:
        candidates.append(base_sha)
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        candidates.extend((f"origin/{base_ref}", base_ref))
    candidates.extend(("origin/main", "origin/master"))

    for candidate in candidates:
        if not _git_ref_exists(candidate):
            continue
        merge_base = subprocess.run(
            ["git", "merge-base", candidate, "HEAD"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            return merge_base.stdout.strip()
        return candidate
    return "HEAD"


def _git_ref_exists(ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_every_legacy_entry_has_classification_row() -> None:
    legacy_ids = {entry.plan_id for entry in _load_index().values() if entry.plan_kind == "legacy"}
    classification = _load_legacy_classification()

    assert set(classification) == legacy_ids


def test_open_root_legacy_requires_retrofit_or_acknowledgment_with_open_snapshot() -> None:
    for row in _load_legacy_classification().values():
        assert _non_empty_string(row.get("legacy_reason")), (
            f"{row['plan_id']} missing legacy_reason"
        )
        assert _non_empty_string(row.get("root_title")), f"{row['plan_id']} missing root_title"
        assert isinstance(row.get("root_open"), bool), f"{row['plan_id']} root_open must be bool"

        retrofit_target = row.get("retrofit_target")
        acknowledgment = row.get("non_retrofit_acknowledgment")
        if row["root_open"]:
            assert bool(retrofit_target) != bool(acknowledgment), (
                f"{row['plan_id']} must have exactly one retrofit or acknowledgment target"
            )
            prefix = "retrofit_target" if retrofit_target else "non_retrofit_acknowledgment"
            assert row.get(f"{prefix}_exists") is True, f"{row['plan_id']} {prefix}_exists"
            assert row.get(f"{prefix}_open") is True, f"{row['plan_id']} {prefix}_open"
            assert _non_empty_string(row.get(f"{prefix}_title")), f"{row['plan_id']} {prefix}_title"
        else:
            assert not (retrofit_target and acknowledgment), (
                f"{row['plan_id']} closed root cannot carry both target types"
            )


def test_legacy_classification_snapshot_matches_live_db_when_available() -> None:
    if not _live_db_enabled():
        return

    for row in _load_legacy_classification().values():
        root_ref = f"#{row['root_task_ref']}"
        root = _live_task(root_ref)
        if not root.exists:
            continue
        assert row["root_open"] is root.open, f"{row['plan_id']} root_open snapshot drift"
        assert row["root_title"] == root.title, f"{row['plan_id']} root_title snapshot drift"

        prefix = _target_prefix(row)
        if prefix is None:
            continue

        target = _live_task(str(row[prefix]))
        assert row[f"{prefix}_exists"] is target.exists, f"{row['plan_id']} {prefix}_exists drift"
        assert row[f"{prefix}_open"] is target.open, f"{row['plan_id']} {prefix}_open drift"
        assert row[f"{prefix}_title"] == target.title, f"{row['plan_id']} {prefix}_title drift"


def test_snapshots_match_live_db_when_available() -> None:
    if not _live_db_enabled() or not _live_db_path().exists():
        return

    env = os.environ.copy()
    env.pop("GOBBY_TEST_PROTECT", None)
    env.pop("GOBBY_HOME", None)
    env.pop("GOBBY_DATABASE_PATH", None)

    for command in ("grandfathered-refresh", "legacy-classification-refresh"):
        result = subprocess.run(
            ["uv", "run", "gobby", "plan", command, "--check"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            f"gobby plan {command} --check failed: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def test_ci_runs_under_no_live_db_with_no_skipped_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_LIVE_DB", "0")
    index = _load_index()

    _assert_index_inventory(index)
    _assert_grandfathered_snapshot()
    _assert_no_orphan_manifests(index)
    for kind in ("strategy", "legacy"):
        _assert_no_manifests_for_plan_kind(kind)
    test_zero_missing_invalid_rows(tmp_path)


def _load_index() -> dict[str, PlanIndexEntry]:
    raw = _load_mapping(INDEX_PATH)
    entries = raw.get("entries")
    assert isinstance(entries, list), "index.yaml entries must be a list"

    index: dict[str, PlanIndexEntry] = {}
    for offset, item in enumerate(entries, start=1):
        assert isinstance(item, dict), f"index entry {offset} must be a mapping"
        entry = PlanIndexEntry(
            plan_id=_required_string(item, "plan_id", path=INDEX_PATH),
            project_id=_required_string(item, "project_id", path=INDEX_PATH),
            root_task_ref=_required_string(item, "root_task_ref", path=INDEX_PATH),
            plan_kind=_required_string(item, "plan_kind", path=INDEX_PATH),
            status=_required_string(item, "status", path=INDEX_PATH),
        )
        assert entry.project_id == PROJECT_ID, f"{entry.plan_id} has wrong project_id"
        assert entry.plan_kind in PLAN_KINDS, f"{entry.plan_id} invalid plan_kind"
        assert entry.status in STATUSES, f"{entry.plan_id} invalid status"
        assert entry.plan_id not in index, f"duplicate plan_id {entry.plan_id}"
        index[entry.plan_id] = entry
    return index


def _discover_plan_ids() -> set[str]:
    return {path.stem for path in PLANS_DIR.glob("*.md") if _is_plan_markdown(path)}


def _is_plan_markdown(path: Path) -> bool:
    if path.name in {"README.md"} or path.name.startswith("."):
        return False
    return path.read_text(encoding="utf-8").lstrip().startswith("#")


def _assert_index_inventory(index: dict[str, PlanIndexEntry]) -> None:
    plan_ids = _discover_plan_ids()
    stale = set(index) - plan_ids
    missing = plan_ids - set(index)

    assert not stale, f"index entries without plan files: {sorted(stale)}"
    assert not missing, f"plan files missing from index: {sorted(missing)}"
    assert set(index) == EXPECTED_PLAN_IDS


def _resolve_parser_kind(plan_kind: str) -> PlanKind:
    if plan_kind == "implementation":
        return PlanKind.implementation
    if plan_kind in {"strategy", "legacy"}:
        return PlanKind.strategy
    raise AssertionError(f"unknown plan_kind {plan_kind!r}")


def _implementation_entries() -> dict[str, PlanIndexEntry]:
    return {
        plan_id: entry
        for plan_id, entry in _load_index().items()
        if entry.plan_kind == "implementation" and entry.status == "active"
    }


def _coverage_manifest_path(entry: PlanIndexEntry) -> Path:
    try:
        from gobby.plans.coverage_manifest import coverage_manifest_path
    except ImportError as exc:  # pragma: no cover - exercised only before A4 lands
        raise AssertionError("missing A4 API: gobby.plans.coverage_manifest") from exc

    return coverage_manifest_path(
        PROJECT_ROOT,
        project_id=entry.project_id,
        root_task_ref=entry.root_task_ref,
        plan_id=entry.plan_id,
    )


def _read_required_manifest(entry: PlanIndexEntry) -> dict[str, Any]:
    manifest_path = _coverage_manifest_path(entry)
    assert manifest_path.exists(), f"missing manifest for {entry.plan_id}: expected {manifest_path}"
    return _load_mapping(manifest_path)


def _manifest_header(manifest: dict[str, Any]) -> dict[str, Any]:
    header = manifest.get("header")
    assert isinstance(header, dict), "coverage manifest missing header mapping"
    return header


def _assert_no_orphan_manifests(index: dict[str, PlanIndexEntry]) -> None:
    implementation_keys = {
        (entry.project_id, entry.root_task_ref, entry.plan_id)
        for entry in index.values()
        if entry.plan_kind == "implementation"
    }

    orphaned: list[str] = []
    for manifest_path in _manifest_paths():
        identity = _manifest_identity(manifest_path)
        if identity not in implementation_keys:
            orphaned.append(str(manifest_path.relative_to(PROJECT_ROOT)))
    assert not orphaned, "orphan or non-implementation manifests: " + ", ".join(orphaned)


def _assert_no_manifests_for_plan_kind(plan_kind: str) -> None:
    entries = {
        (entry.project_id, entry.root_task_ref, entry.plan_id)
        for entry in _load_index().values()
        if entry.plan_kind == plan_kind
    }
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in _manifest_paths()
        if _manifest_identity(path) in entries
    ]
    assert not offenders, f"{plan_kind} plans must not have manifests: {offenders}"


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


def _grandfathered_entries() -> list[GrandfatheredEntry]:
    assert GRANDFATHERED_PATH.exists(), ".grandfathered file missing"
    entries: list[GrandfatheredEntry] = []
    for line_number, line in enumerate(
        GRANDFATHERED_PATH.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = REMOVE_BY_RE.search(line)
        remove_by = _normalize_ref(match.group("ref")) if match else None
        entries.append(GrandfatheredEntry(line_number, line, remove_by))
    return entries


def _assert_grandfathered_snapshot() -> None:
    entries = _grandfathered_entries()
    refs = {entry.remove_by for entry in entries if entry.remove_by is not None}
    snapshot = _load_grandfathered_snapshot()
    snapshot_refs = {str(item["task_ref"]) for item in snapshot["refs"]}

    assert snapshot_refs == refs
    for item in snapshot["refs"]:
        task_ref = str(item["task_ref"])
        assert item["exists"] is True, f"{task_ref} snapshot exists must be true"
        assert item["open"] is True, f"{task_ref} snapshot open must be true"
        assert _non_empty_string(item.get("title")), f"{task_ref} snapshot title required"


def _load_grandfathered_snapshot() -> dict[str, Any]:
    snapshot = _load_mapping(GRANDFATHERED_STATE_PATH)
    refs = snapshot.get("refs")
    assert isinstance(refs, list), ".grandfathered-task-state.yaml refs must be a list"
    for item in refs:
        assert isinstance(item, dict), "grandfathered snapshot rows must be mappings"
        assert _normalize_ref(_required_string(item, "task_ref", path=GRANDFATHERED_STATE_PATH))
        assert isinstance(item.get("exists"), bool), "snapshot exists must be bool"
        assert isinstance(item.get("open"), bool), "snapshot open must be bool"
        assert _non_empty_string(item.get("title")), "snapshot title must be non-empty"
    return snapshot


def _load_legacy_classification() -> dict[str, dict[str, Any]]:
    raw = _load_mapping(LEGACY_CLASSIFICATION_PATH)
    entries = raw.get("entries")
    assert isinstance(entries, list), ".legacy-classification.yaml entries must be a list"

    rows: dict[str, dict[str, Any]] = {}
    for item in entries:
        assert isinstance(item, dict), "legacy classification rows must be mappings"
        plan_id = _required_string(item, "plan_id", path=LEGACY_CLASSIFICATION_PATH)
        assert plan_id not in rows, f"duplicate legacy classification row {plan_id}"
        _required_string(item, "root_task_ref", path=LEGACY_CLASSIFICATION_PATH)
        rows[plan_id] = item
    return rows


def _target_prefix(row: dict[str, Any]) -> str | None:
    if row.get("retrofit_target"):
        return "retrofit_target"
    if row.get("non_retrofit_acknowledgment"):
        return "non_retrofit_acknowledgment"
    return None


def _live_db_enabled() -> bool:
    return os.environ.get("GOBBY_LIVE_DB", "1") != "0"


def _live_task(task_ref: str) -> LiveTask:
    db_path = _live_db_path()
    if not db_path.exists():
        return LiveTask(exists=False, open=False, title="")

    seq_num = _seq_num(task_ref)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            select title, status, closed_at
            from tasks
            where project_id = ? and seq_num = ?
            """,
            (PROJECT_ID, seq_num),
        ).fetchone()
    if row is None:
        return LiveTask(exists=False, open=False, title="")
    title, status, closed_at = row
    return LiveTask(exists=True, open=status != "closed" and closed_at is None, title=str(title))


def _live_db_path() -> Path:
    return Path(os.environ.get("GOBBY_HUB_DB", "~/.gobby/gobby-hub.db")).expanduser()


def _load_mapping(path: Path) -> dict[str, Any]:
    assert path.exists(), f"missing required file: {path.relative_to(PROJECT_ROOT)}"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict), f"{path.relative_to(PROJECT_ROOT)} must be a YAML mapping"
    return raw


def _required_string(raw: dict[str, Any], key: str, *, path: Path) -> str:
    value = raw.get(key)
    assert isinstance(value, str) and value.strip(), (
        f"{path.relative_to(PROJECT_ROOT)} missing non-empty {key}"
    )
    return value


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_ref(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("#"):
        return stripped
    assert stripped.isdecimal(), f"invalid task ref {value!r}"
    return f"#{stripped}"


def _seq_num(task_ref: str) -> int:
    normalized = _normalize_ref(task_ref)
    return int(normalized[1:])
