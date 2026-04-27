"""Plan snapshot refresh commands."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import click
import yaml

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.tasks import LocalTaskManager

PLANS_DIR = Path(".gobby") / "plans"
GRANDFATHERED_PATH = PLANS_DIR / ".grandfathered"
GRANDFATHERED_STATE_PATH = PLANS_DIR / ".grandfathered-task-state.yaml"
LEGACY_CLASSIFICATION_PATH = PLANS_DIR / ".legacy-classification.yaml"
PLAN_INDEX_PATH = PLANS_DIR / "index.yaml"

GRANDFATHERED_GENERATOR = "gobby plan grandfathered-refresh"
LEGACY_GENERATOR = "gobby plan legacy-classification-refresh"

REMOVE_BY_RE = re.compile(r"#\s*remove-by:\s*(?P<ref>#?\d+)\b")
TARGET_PREFIXES = ("retrofit_target", "non_retrofit_acknowledgment")


class TaskManagerLike(Protocol):
    def resolve_task_reference(self, reference: str, project_id: str) -> str: ...

    def get_task(self, task_id: str, project_id: str | None = None) -> Any: ...


@dataclass(frozen=True)
class TaskSnapshot:
    exists: bool
    open: bool
    title: str


def grandfathered_refresh(*, check: bool) -> None:
    """Regenerate the grandfathered task-state snapshot."""
    repo_root = Path.cwd()
    manager = _task_manager_from_live_db()
    existing = _load_yaml_mapping(repo_root / GRANDFATHERED_STATE_PATH)
    generated_at = _generated_at_for_mode(existing, check=check)
    rendered = render_grandfathered_snapshot(
        repo_root=repo_root,
        task_manager=manager,
        generated_at=generated_at,
    )
    _write_or_check(repo_root / GRANDFATHERED_STATE_PATH, rendered, check=check)


def legacy_classification_refresh(*, check: bool) -> None:
    """Regenerate the legacy-classification snapshot."""
    repo_root = Path.cwd()
    manager = _task_manager_from_live_db()
    existing = _load_yaml_mapping(repo_root / LEGACY_CLASSIFICATION_PATH)
    generated_at = _generated_at_for_mode(existing, check=check)
    rendered = render_legacy_classification_snapshot(
        repo_root=repo_root,
        task_manager=manager,
        existing_snapshot=existing,
        generated_at=generated_at,
    )
    _write_or_check(repo_root / LEGACY_CLASSIFICATION_PATH, rendered, check=check)


@click.command("grandfathered-refresh")
@click.option("--check", is_flag=True, help="Fail if the committed snapshot is stale.")
def grandfathered_refresh_command(check: bool) -> None:
    """Refresh .gobby/plans/.grandfathered-task-state.yaml."""
    grandfathered_refresh(check=check)


@click.command("legacy-classification-refresh")
@click.option("--check", is_flag=True, help="Fail if the committed snapshot is stale.")
def legacy_classification_refresh_command(check: bool) -> None:
    """Refresh .gobby/plans/.legacy-classification.yaml."""
    legacy_classification_refresh(check=check)


def render_grandfathered_snapshot(
    *,
    repo_root: Path,
    task_manager: TaskManagerLike,
    generated_at: str,
) -> str:
    project_id = _project_id_from_index(repo_root / PLAN_INDEX_PATH)
    refs = sorted(_remove_by_refs(repo_root / GRANDFATHERED_PATH), key=_ref_sort_key)
    data: dict[str, Any] = {
        "generated_at": generated_at,
        "generator": GRANDFATHERED_GENERATOR,
        "refs": [
            {
                "task_ref": ref,
                "exists": (snapshot := _task_snapshot(task_manager, ref, project_id)).exists,
                "open": snapshot.open,
                "title": snapshot.title,
            }
            for ref in refs
        ],
    }
    return _dump_yaml(data)


def render_legacy_classification_snapshot(
    *,
    repo_root: Path,
    task_manager: TaskManagerLike,
    existing_snapshot: dict[str, Any],
    generated_at: str,
) -> str:
    legacy_entries = sorted(
        (
            entry
            for entry in _index_entries(repo_root / PLAN_INDEX_PATH)
            if entry.get("plan_kind") == "legacy"
        ),
        key=lambda item: str(item["plan_id"]),
    )
    existing_rows = _existing_legacy_rows(existing_snapshot)
    rows = [
        _legacy_row(entry, existing_rows.get(str(entry["plan_id"])), task_manager)
        for entry in legacy_entries
    ]
    data: dict[str, Any] = {
        "generated_at": generated_at,
        "generator": LEGACY_GENERATOR,
        "entries": rows,
    }
    return _dump_yaml(data)


def _legacy_row(
    index_entry: dict[str, Any],
    existing: dict[str, Any] | None,
    task_manager: TaskManagerLike,
) -> dict[str, Any]:
    plan_id = _required_string(index_entry, "plan_id", path=PLAN_INDEX_PATH)
    project_id = _required_string(index_entry, "project_id", path=PLAN_INDEX_PATH)
    root_task_ref = _required_string(index_entry, "root_task_ref", path=PLAN_INDEX_PATH)
    if existing is None:
        raise click.ClickException(
            f"{LEGACY_CLASSIFICATION_PATH} missing row for legacy plan {plan_id}"
        )

    root = _task_snapshot(task_manager, _normalize_ref(root_task_ref), project_id)
    row: dict[str, Any] = {
        "plan_id": plan_id,
        "root_task_ref": root_task_ref,
        "root_open": root.open,
        "root_title": root.title,
        "legacy_reason": _required_string(
            existing, "legacy_reason", path=LEGACY_CLASSIFICATION_PATH
        ),
    }

    target_prefix = _existing_target_prefix(existing)
    if target_prefix is None:
        return row

    target_ref = _required_string(existing, target_prefix, path=LEGACY_CLASSIFICATION_PATH)
    target = _task_snapshot(task_manager, target_ref, project_id)
    row[target_prefix] = target_ref
    row[f"{target_prefix}_exists"] = target.exists
    row[f"{target_prefix}_open"] = target.open
    row[f"{target_prefix}_title"] = target.title
    return row


def _task_snapshot(
    task_manager: TaskManagerLike,
    task_ref: str,
    project_id: str,
) -> TaskSnapshot:
    try:
        task_id = task_manager.resolve_task_reference(task_ref, project_id)
    except Exception:
        return TaskSnapshot(exists=False, open=False, title="")
    task = task_manager.get_task(task_id)
    if task is None:
        return TaskSnapshot(exists=False, open=False, title="")
    return TaskSnapshot(
        exists=True,
        open=task.status != "closed" and task.closed_at is None,
        title=task.title,
    )


def _task_manager_from_live_db() -> LocalTaskManager:
    db = LocalDatabase()
    run_migrations(db)
    return LocalTaskManager(db)


def _write_or_check(path: Path, rendered: str, *, check: bool) -> None:
    if check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != rendered:
            raise click.ClickException(
                f"{path} is stale; run the corresponding gobby plan refresh command"
            )
        click.echo(f"{path} is up to date")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    click.echo(path)


def _generated_at_for_mode(existing: dict[str, Any], *, check: bool) -> str:
    if check and isinstance(existing.get("generated_at"), str):
        return str(existing["generated_at"])
    return _utc_now()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _remove_by_refs(path: Path) -> set[str]:
    if not path.exists():
        return set()
    refs: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = REMOVE_BY_RE.search(line)
        if match:
            refs.add(_normalize_ref(match.group("ref")))
    return refs


def _project_id_from_index(path: Path) -> str:
    entries = _index_entries(path)
    project_ids = {
        _required_string(entry, "project_id", path=path)
        for entry in entries
        if isinstance(entry, dict)
    }
    if len(project_ids) != 1:
        raise click.ClickException(f"{path} must contain exactly one project_id")
    return next(iter(project_ids))


def _index_entries(path: Path) -> list[dict[str, Any]]:
    raw = _load_yaml_mapping(path)
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise click.ClickException(f"{path} entries must be a list")
    if not all(isinstance(entry, dict) for entry in entries):
        raise click.ClickException(f"{path} entries must be mappings")
    return entries


def _existing_legacy_rows(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("entries", [])
    if not isinstance(rows, list):
        raise click.ClickException(f"{LEGACY_CLASSIFICATION_PATH} entries must be a list")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise click.ClickException(f"{LEGACY_CLASSIFICATION_PATH} rows must be mappings")
        plan_id = _required_string(row, "plan_id", path=LEGACY_CLASSIFICATION_PATH)
        result[plan_id] = row
    return result


def _existing_target_prefix(row: dict[str, Any]) -> str | None:
    present = [prefix for prefix in TARGET_PREFIXES if row.get(prefix)]
    if len(present) > 1:
        raise click.ClickException(
            f"{LEGACY_CLASSIFICATION_PATH} row {row.get('plan_id')} has multiple target fields"
        )
    return present[0] if present else None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise click.ClickException(f"{path} must be a YAML mapping")
    return raw


def _required_string(raw: dict[str, Any], key: str, *, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise click.ClickException(f"{path} missing non-empty {key}")
    return value


def _normalize_ref(task_ref: str) -> str:
    stripped = task_ref.strip()
    return stripped if stripped.startswith("#") else f"#{stripped}"


def _ref_sort_key(task_ref: str) -> tuple[int, str]:
    digits = task_ref.removeprefix("#")
    if digits.isdigit():
        return (int(digits), task_ref)
    return (10**12, task_ref)


def _dump_yaml(data: dict[str, Any]) -> str:
    if data.get("generator") == GRANDFATHERED_GENERATOR:
        return _dump_grandfathered_yaml(data)
    if data.get("generator") == LEGACY_GENERATOR:
        return _dump_legacy_yaml(data)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120)


def _dump_grandfathered_yaml(data: dict[str, Any]) -> str:
    refs = data["refs"]
    lines = [
        f"generated_at: {_quote(data['generated_at'])}",
        f"generator: {_quote(data['generator'])}",
    ]
    if not refs:
        lines.append("refs: []")
        return "\n".join(lines) + "\n"

    lines.append("refs:")
    for row in refs:
        lines.extend(
            [
                f"  - task_ref: {_quote(row['task_ref'])}",
                f"    exists: {_bool(row['exists'])}",
                f"    open: {_bool(row['open'])}",
                f"    title: {_quote(row['title'])}",
            ]
        )
    return "\n".join(lines) + "\n"


def _dump_legacy_yaml(data: dict[str, Any]) -> str:
    lines = [
        f"generated_at: {_quote(data['generated_at'])}",
        f"generator: {_quote(data['generator'])}",
    ]
    entries = data["entries"]
    if not entries:
        lines.append("entries: []")
        return "\n".join(lines) + "\n"

    lines.append("entries:")
    for row in entries:
        lines.extend(
            [
                f"  - plan_id: {row['plan_id']}",
                f"    root_task_ref: {_quote(row['root_task_ref'])}",
                f"    root_open: {_bool(row['root_open'])}",
                f"    root_title: {_quote(row['root_title'])}",
                f"    legacy_reason: {_quote(row['legacy_reason'])}",
            ]
        )
        for prefix in TARGET_PREFIXES:
            if prefix not in row:
                continue
            lines.extend(
                [
                    f"    {prefix}: {_quote(row[prefix])}",
                    f"    {prefix}_exists: {_bool(row[f'{prefix}_exists'])}",
                    f"    {prefix}_open: {_bool(row[f'{prefix}_open'])}",
                    f"    {prefix}_title: {_quote(row[f'{prefix}_title'])}",
                ]
            )
    return "\n".join(lines) + "\n"


def _quote(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _bool(value: object) -> str:
    return "true" if bool(value) else "false"


__all__ = [
    "grandfathered_refresh_command",
    "legacy_classification_refresh_command",
    "render_grandfathered_snapshot",
    "render_legacy_classification_snapshot",
]
