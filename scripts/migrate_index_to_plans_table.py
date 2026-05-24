#!/usr/bin/env python3
"""One-shot migration from .gobby/plans/index.yaml to the plans table."""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from gobby.plans.parser import parse_plan
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--database")
    parser.add_argument("--keep-index", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    db = _open_db(args.database)
    try:
        count = migrate(repo_root, db, delete_index=not args.keep_index)
    finally:
        db.close()
    print(f"migrated {count} plan(s)")
    return 0


def migrate(repo_root: Path, db: HubDatabase, *, delete_index: bool = True) -> int:
    entries = _index_entries(repo_root / ".gobby" / "plans" / "index.yaml")
    count = 0
    for path in _plan_files(repo_root):
        entry = entries.get(path.stem, {})
        plan_doc = parse_plan(path, parse_mode="draft")
        project_id = str(entry.get("project_id") or _project_id(repo_root))
        root_task_ref = _normalize_task_ref(entry.get("root_task_ref") or _root_ref(path) or "")
        if not root_task_ref:
            logger.warning(
                "Skipping plan without root_task_ref (project_id=%s, path=%s)",
                project_id,
                path,
            )
            continue
        state = "archived" if path.parent.name == "completed" else "active"
        _upsert_plan(
            db,
            project_id=project_id,
            plan_id=plan_doc.plan_id or path.stem,
            plan_path=str(path.relative_to(repo_root)),
            plan_hash=plan_doc.source_hash,
            plan_kind=_normalize_plan_kind(str(entry.get("plan_kind") or "implementation")),
            state=state,
            root_task_ref=root_task_ref,
        )
        count += 1
    index_path = repo_root / ".gobby" / "plans" / "index.yaml"
    if delete_index and index_path.exists():
        index_path.unlink()
    return count


def _plan_files(repo_root: Path) -> list[Path]:
    plans_dir = repo_root / ".gobby" / "plans"
    return sorted(plans_dir.glob("*.md")) + sorted((plans_dir / "completed").glob("*.md"))


def _open_db(database_url: str | None) -> HubDatabase:
    if database_url:
        from gobby.storage.hub.postgres import PostgresHubDatabase

        db = PostgresHubDatabase(database_url)
        try:
            db.apply_migrations()
        except Exception:
            db.close()
            raise
        return db

    from gobby.storage.hub.runtime import open_runtime_hub_database

    return open_runtime_hub_database()


def _upsert_plan(
    db: HubDatabase,
    *,
    project_id: str,
    plan_id: str,
    plan_path: str,
    plan_hash: str,
    plan_kind: str,
    state: str,
    root_task_ref: str,
) -> None:
    now = _now()
    archived_at = now if state == "archived" else None
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO plans (
                id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
                root_task_ref, created_at, updated_at, archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, plan_id) DO UPDATE SET
                plan_path = excluded.plan_path,
                plan_hash = excluded.plan_hash,
                plan_kind = excluded.plan_kind,
                state = excluded.state,
                root_task_ref = excluded.root_task_ref,
                updated_at = excluded.updated_at,
                archived_at = excluded.archived_at
            """,
            (
                str(uuid.uuid4()),
                project_id,
                plan_id,
                plan_path,
                plan_hash,
                plan_kind,
                state,
                root_task_ref,
                now,
                now,
                archived_at,
            ),
        )


def _normalize_plan_kind(value: str) -> str:
    return "strategy" if value == "legacy" else value


def _normalize_task_ref(value: Any) -> str:
    ref = str(value or "").strip()
    if ref.isdecimal():
        return f"#{ref}"
    return ref


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _index_entries(index_path: Path) -> dict[str, dict[str, Any]]:
    if not index_path.exists():
        return {}
    raw = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in entries:
        if isinstance(item, dict) and item.get("plan_id"):
            result[str(item["plan_id"])] = item
    return result


def _project_id(repo_root: Path) -> str:
    path = repo_root / ".gobby" / "project.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing project config: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed project config: {path}: {exc}") from exc
    if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
        raise ValueError(f"Malformed project config: {path}: missing id")
    return str(raw["id"]).strip()


def _root_ref(path: Path) -> str | None:
    stem = path.stem
    if stem.startswith("task-"):
        token = stem.split("-", 2)[1]
        if token.isdecimal():
            return f"#{token}"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
