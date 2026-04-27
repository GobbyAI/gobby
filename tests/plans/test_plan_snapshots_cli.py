from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from gobby.cli import cli, plan_snapshots

pytestmark = pytest.mark.unit

FIXED_GENERATED_AT = "2026-04-27T17:00:00Z"
PROJECT_ID = "project-1"


@dataclass(frozen=True)
class FakeTask:
    title: str
    status: str = "open"
    closed_at: str | None = None


class FakeTaskManager:
    def __init__(self, tasks: dict[str, FakeTask]) -> None:
        self.tasks = tasks

    def resolve_task_reference(self, reference: str, project_id: str) -> str:
        assert project_id == PROJECT_ID
        normalized = reference if reference.startswith("#") else f"#{reference}"
        if normalized not in self.tasks:
            raise ValueError(f"unknown task {reference}")
        return normalized

    def get_task(self, task_id: str, project_id: str | None = None) -> FakeTask | None:
        return self.tasks.get(task_id)


def test_grandfathered_refresh_generates_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_index(tmp_path, [{"plan_id": "task-1", "root_task_ref": "1", "plan_kind": "strategy"}])
    _plans_dir(tmp_path, ".grandfathered").write_text(
        "task-legacy.md  # remove-by: #20\nother.md  # remove-by: 10\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_snapshots, "_utc_now", lambda: FIXED_GENERATED_AT)
    monkeypatch.setattr(
        plan_snapshots,
        "_task_manager_from_live_db",
        lambda: FakeTaskManager(
            {
                "#10": FakeTask("Remove first"),
                "#20": FakeTask("Remove second"),
            }
        ),
    )

    result = CliRunner().invoke(cli, ["plan", "grandfathered-refresh"])

    assert result.exit_code == 0, result.output
    snapshot = yaml.safe_load(_plans_dir(tmp_path, ".grandfathered-task-state.yaml").read_text())
    assert snapshot == {
        "generated_at": FIXED_GENERATED_AT,
        "generator": "gobby plan grandfathered-refresh",
        "refs": [
            {"task_ref": "#10", "exists": True, "open": True, "title": "Remove first"},
            {"task_ref": "#20", "exists": True, "open": True, "title": "Remove second"},
        ],
    }


def test_legacy_classification_refresh_generates_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_index(
        tmp_path,
        [
            {"plan_id": "task-open", "root_task_ref": "100", "plan_kind": "legacy"},
            {"plan_id": "task-closed", "root_task_ref": "200", "plan_kind": "legacy"},
            {"plan_id": "task-active", "root_task_ref": "300", "plan_kind": "implementation"},
        ],
    )
    _plans_dir(tmp_path, ".legacy-classification.yaml").write_text(
        yaml.safe_dump(
            {
                "generated_at": "2026-04-27T16:00:00Z",
                "generator": "gobby plan legacy-classification-refresh",
                "entries": [
                    {
                        "plan_id": "task-open",
                        "root_task_ref": "100",
                        "root_open": False,
                        "root_title": "stale",
                        "legacy_reason": "Historical plan with acknowledged disposition.",
                        "non_retrofit_acknowledgment": "#101",
                        "non_retrofit_acknowledgment_exists": False,
                        "non_retrofit_acknowledgment_open": False,
                        "non_retrofit_acknowledgment_title": "stale",
                    },
                    {
                        "plan_id": "task-closed",
                        "root_task_ref": "200",
                        "root_open": True,
                        "root_title": "stale",
                        "legacy_reason": "Closed historical root.",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_snapshots, "_utc_now", lambda: FIXED_GENERATED_AT)
    monkeypatch.setattr(
        plan_snapshots,
        "_task_manager_from_live_db",
        lambda: FakeTaskManager(
            {
                "#100": FakeTask("Open root"),
                "#101": FakeTask("Acknowledgment"),
                "#200": FakeTask("Closed root", status="closed", closed_at="2026-04-26T00:00:00Z"),
            }
        ),
    )

    result = CliRunner().invoke(cli, ["plan", "legacy-classification-refresh"])

    assert result.exit_code == 0, result.output
    snapshot = yaml.safe_load(_plans_dir(tmp_path, ".legacy-classification.yaml").read_text())
    assert snapshot == {
        "generated_at": FIXED_GENERATED_AT,
        "generator": "gobby plan legacy-classification-refresh",
        "entries": [
            {
                "plan_id": "task-closed",
                "root_task_ref": "200",
                "root_open": False,
                "root_title": "Closed root",
                "legacy_reason": "Closed historical root.",
            },
            {
                "plan_id": "task-open",
                "root_task_ref": "100",
                "root_open": True,
                "root_title": "Open root",
                "legacy_reason": "Historical plan with acknowledged disposition.",
                "non_retrofit_acknowledgment": "#101",
                "non_retrofit_acknowledgment_exists": True,
                "non_retrofit_acknowledgment_open": True,
                "non_retrofit_acknowledgment_title": "Acknowledgment",
            },
        ],
    }


def test_refresh_is_deterministic_for_fixed_db_state(tmp_path: Path) -> None:
    _write_index(
        tmp_path,
        [
            {"plan_id": "task-b", "root_task_ref": "20", "plan_kind": "legacy"},
            {"plan_id": "task-a", "root_task_ref": "10", "plan_kind": "legacy"},
        ],
    )
    existing = {
        "entries": [
            {
                "plan_id": "task-b",
                "root_task_ref": "20",
                "root_open": False,
                "root_title": "stale",
                "legacy_reason": "B reason.",
            },
            {
                "plan_id": "task-a",
                "root_task_ref": "10",
                "root_open": False,
                "root_title": "stale",
                "legacy_reason": "A reason.",
            },
        ]
    }
    manager = FakeTaskManager({"#10": FakeTask("A root"), "#20": FakeTask("B root")})

    first = plan_snapshots.render_legacy_classification_snapshot(
        repo_root=tmp_path,
        task_manager=manager,
        existing_snapshot=existing,
        generated_at=FIXED_GENERATED_AT,
    )
    second = plan_snapshots.render_legacy_classification_snapshot(
        repo_root=tmp_path,
        task_manager=manager,
        existing_snapshot=existing,
        generated_at=FIXED_GENERATED_AT,
    )

    assert first == second
    assert [row["plan_id"] for row in yaml.safe_load(first)["entries"]] == ["task-a", "task-b"]


def test_refresh_subcommands_registered_in_plan_cli() -> None:
    result = CliRunner().invoke(cli, ["plan", "--help"])

    assert result.exit_code == 0
    assert "grandfathered-refresh" in result.output
    assert "legacy-classification-refresh" in result.output


def _write_index(tmp_path: Path, entries: list[dict[str, str]]) -> None:
    normalized = []
    for entry in entries:
        normalized.append(
            {
                "plan_id": entry["plan_id"],
                "project_id": PROJECT_ID,
                "root_task_ref": entry["root_task_ref"],
                "plan_kind": entry["plan_kind"],
                "status": entry.get("status", "active"),
            }
        )
    _plans_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    _plans_dir(tmp_path, "index.yaml").write_text(
        yaml.safe_dump({"entries": normalized}, sort_keys=False),
        encoding="utf-8",
    )


def _plans_dir(tmp_path: Path, name: str | None = None) -> Path:
    base = tmp_path / ".gobby" / "plans"
    return base if name is None else base / name
