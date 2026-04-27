from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from gobby.cli import cli
from gobby.plans.coverage import (
    CoverageHeader,
    CoverageReport,
    CoverageRow,
    CoverageStatus,
    TaskTreeSource,
)
from gobby.plans.coverage_manifest import write_manifest

pytestmark = pytest.mark.unit


def _plan_file(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\nPlan ID: plan\n", encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _matrix_file(tmp_path: Path, status: str, *, plan_hash: str) -> Path:
    path = tmp_path / f"{status}.coverage.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "header": {
                    "plan_id": "plan",
                    "plan_hash": plan_hash,
                    "root_task_ref": "#1",
                    "project_id": "project",
                },
                "rows": [{"section_id": "A1", "item_id": "A1.1", "status": status}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _base_args(plan_path: Path, plan_hash: str, matrix: Path, manifest: Path) -> list[str]:
    return [
        "plan",
        "coverage",
        "--plan",
        str(plan_path),
        "--plan-id",
        "plan",
        "--plan-hash",
        plan_hash,
        "--task-tree",
        "matrix-file",
        "--matrix-file",
        str(matrix),
        "--manifest",
        str(manifest),
    ]


def test_cli_help_lists_exact_ten_flags() -> None:
    result = CliRunner().invoke(cli, ["plan", "coverage", "--help"])

    assert result.exit_code == 0
    expected = {
        "--plan",
        "--plan-id",
        "--plan-hash",
        "--task-tree",
        "--root-task",
        "--project-id",
        "--matrix-file",
        "--evidence",
        "--manifest",
        "--regenerate",
    }
    options = {
        token.rstrip(",")
        for token in result.output.replace("[required]", "").split()
        if token.startswith("--") and token != "--help"
    }
    assert options == expected
    for required in ("--plan", "--plan-id", "--plan-hash", "--task-tree"):
        line = next(
            (line for line in result.output.splitlines() if required in line),
            None,
        )
        assert line is not None, f"Flag {required} not present in output"
        assert "[required]" in line, f"Flag {required} missing [required] marker"


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        ("covered", 0),
        ("deferred", 0),
        ("missing", 2),
        ("invalid", 3),
    ],
)
def test_cli_exit_codes_per_status(tmp_path: Path, status: str, expected_exit: int) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, status, plan_hash=plan_hash)
    manifest = tmp_path / f"out-{status}.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == expected_exit


def test_cli_writes_evidence_from_flag(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, "covered", plan_hash=plan_hash)
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(
        cli,
        [
            *_base_args(plan_path, plan_hash, matrix, manifest),
            "--evidence",
            "none",
        ],
    )

    assert result.exit_code == 0
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["header"]["evidence_summary"] == ["none:none:resolved"]
    assert raw["rows"][0]["evidence"] == [
        {
            "kind": "none",
            "ref": "none",
            "status": "resolved",
            "detail": "explicit operator override",
            "artifacts_touched": [],
        }
    ]


def test_cli_normalizes_root_task_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    manifest = tmp_path / "out.coverage.yaml"

    def fake_evaluate(**kwargs: object) -> CoverageReport:
        root_task_ref = kwargs["root_task_ref"]
        assert isinstance(root_task_ref, str)
        return CoverageReport(
            header=CoverageHeader(
                plan_id="plan",
                plan_hash=plan_hash,
                root_task_ref=root_task_ref,
                project_id="project",
                generated_at="2026-04-27T00:00:00Z",
                task_tree_source=TaskTreeSource.db,
                task_tree_source_hash="tree",
                evidence_summary=(),
            ),
            rows=(
                CoverageRow(
                    section_id="A1",
                    item_id="A1.1",
                    status=CoverageStatus.covered,
                ),
            ),
        )

    plan_module = importlib.import_module("gobby.cli.plan")
    monkeypatch.setattr(plan_module, "evaluate", fake_evaluate)

    result = CliRunner().invoke(
        cli,
        [
            "plan",
            "coverage",
            "--plan",
            str(plan_path),
            "--plan-id",
            "plan",
            "--plan-hash",
            plan_hash,
            "--task-tree",
            "db",
            "--root-task",
            "#12725",
            "--project-id",
            "project",
            "--manifest",
            str(manifest),
        ],
    )

    assert result.exit_code == 0
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["header"]["root_task_ref"] == "12725"


def test_cli_exit_codes_for_errors(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    stale_matrix = _matrix_file(tmp_path, "covered", plan_hash="old")
    manifest = tmp_path / "out.coverage.yaml"
    stale = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, stale_matrix, manifest))
    assert stale.exit_code == 4

    missing_scope = CliRunner().invoke(
        cli,
        [
            "plan",
            "coverage",
            "--plan",
            str(plan_path),
            "--plan-id",
            "plan",
            "--plan-hash",
            plan_hash,
            "--task-tree",
            "db",
        ],
    )
    assert missing_scope.exit_code == 6

    empty_component = CliRunner().invoke(
        cli,
        [
            "plan",
            "coverage",
            "--plan",
            str(plan_path),
            "--plan-id",
            "///",
            "--plan-hash",
            plan_hash,
            "--task-tree",
            "matrix-file",
            "--matrix-file",
            str(_matrix_file(tmp_path, "covered", plan_hash=plan_hash)),
        ],
    )
    assert empty_component.exit_code == 7


def test_identity_collision_emits_exit_5(tmp_path: Path) -> None:
    report = CoverageReport(
        header=CoverageHeader(
            plan_id="plan",
            plan_hash="old",
            root_task_ref="#1",
            project_id="project",
            generated_at="2026-04-27T00:00:00Z",
            task_tree_source=TaskTreeSource.db,
            task_tree_source_hash="tree",
            evidence_summary=(),
        ),
        rows=(CoverageRow(section_id="A1", item_id="A1.1", status=CoverageStatus.covered),),
    )
    manifest = write_manifest(report, tmp_path)
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, "covered", plan_hash=plan_hash)

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 5


def test_path_identity_mismatch_emits_exit_8(tmp_path: Path) -> None:
    report = CoverageReport(
        header=CoverageHeader(
            plan_id="other",
            plan_hash="hash",
            root_task_ref="#1",
            project_id="project",
            generated_at="2026-04-27T00:00:00Z",
            task_tree_source=TaskTreeSource.db,
            task_tree_source_hash="tree",
            evidence_summary=(),
        ),
        rows=(CoverageRow(section_id="A1", item_id="A1.1", status=CoverageStatus.covered),),
    )
    manifest = write_manifest(report, tmp_path)
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, "covered", plan_hash=plan_hash)

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 8
