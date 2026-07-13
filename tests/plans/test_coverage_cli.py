from __future__ import annotations

import hashlib
import importlib
import re
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
    _plan_node_hash,
    evaluate,
)
from gobby.plans.coverage_manifest import write_manifest
from gobby.plans.parser import parse_plan

pytestmark = pytest.mark.unit


def _plan_file(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "plan.md"
    path.write_text(
        """> **Plan ID:** plan

## A1 Work [category: code]
`kind: deliverable`

Implement the covered behavior.

**Acceptance:**
- A1.1 - Behavior exists. file: `src/behavior.py`
- A1.2 - Behavior is documented. file: `docs/behavior.md`
""",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _matrix_file(
    tmp_path: Path,
    status: str,
    *,
    plan_hash: str | None,
    rows: list[dict[str, object]] | None = None,
) -> Path:
    path = tmp_path / f"{status}.coverage.yaml"
    plan_doc = parse_plan(tmp_path / "plan.md", parse_mode="draft")
    section = next(section for section in plan_doc.sections if section.section_id == "A1")
    header: dict[str, object] = {
        "plan_id": "plan",
        "root_task_ref": "#1",
        "project_id": "project",
    }
    if plan_hash is not None:
        header["plan_hash"] = plan_hash
    matrix_rows = rows
    if matrix_rows is None:
        matrix_rows = [
            {
                "section_id": "A1",
                "item_id": item.item_id,
                "plan_node_hash": _plan_node_hash(section, item),
                "status": status,
            }
            for item in section.acceptance_items
        ]
    path.write_text(
        yaml.safe_dump(
            {
                "header": header,
                "rows": matrix_rows,
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
    options = set(re.findall(r"^  (--[a-z-]+)(?=\s|\[)", result.output, flags=re.MULTILINE))
    options.discard("--help")
    assert options == expected
    option_blocks = re.split(r"(?=^  --)", result.output, flags=re.MULTILINE)
    for required in ("--plan", "--plan-id", "--plan-hash", "--task-tree"):
        block = next(
            (block for block in option_blocks if block.startswith(f"  {required}")),
            None,
        )
        assert block is not None, f"Flag {required} not present in output"
        assert "[required]" in block, f"Flag {required} missing [required] marker"


def test_cli_help_describes_task_tree_modes_and_inputs() -> None:
    result = CliRunner().invoke(cli, ["plan", "coverage", "--help"])
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "--task-tree [db|matrix-file]" in normalized_output
    assert "db reads live tasks; matrix-file reads" in normalized_output
    assert "required for db mode" in normalized_output
    assert "Coverage matrix YAML/JSON" in normalized_output
    assert "Writes a coverage manifest and prints its path" in normalized_output
    assert "jsonl" not in normalized_output.lower()


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


def test_cli_malformed_plan_exits_with_invalid_input_code(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        """> **Plan ID:** plan

## A1 Work [category: code]
`kind: deliverable`

This section has no acceptance block.
""",
        encoding="utf-8",
    )
    plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    matrix = tmp_path / "input.coverage.yaml"
    matrix.write_text("rows: []\n", encoding="utf-8")
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 3
    assert "Error:" in result.output
    assert "missing **Acceptance:** block" in result.output
    assert "Traceback" not in result.output


def test_cli_malformed_matrix_exits_with_invalid_input_code(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = tmp_path / "broken.coverage.yaml"
    matrix.write_text("rows: [", encoding="utf-8")
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 3
    assert "Error:" in result.output
    assert "while parsing a flow node" in result.output
    assert "Traceback" not in result.output


def test_cli_missing_matrix_file_fails_click_validation(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = tmp_path / "missing.coverage.yaml"
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 2
    assert "Invalid value for '--matrix-file'" in result.output
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("label", "expected_section"),
    [
        ("covers:plan:A9:A9.1", "A9"),
        ("covers:plan:A1", "<invalid-covers-label>"),
    ],
    ids=["dangling", "malformed"],
)
def test_cli_fails_for_invalid_covers_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    expected_section: str,
) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {"ref": "#1", "path_cache": "1"},
            {
                "ref": "#2",
                "path_cache": "1.2",
                "labels": [label],
            },
        ],
    )
    plan_module = importlib.import_module("gobby.cli.plan")
    monkeypatch.setattr(plan_module, "evaluate", lambda **_kwargs: report)
    manifest = tmp_path / "dangling.coverage.yaml"

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
            "#1",
            "--project-id",
            "project",
            "--manifest",
            str(manifest),
        ],
    )

    assert result.exit_code == 3
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert any(
        row["section_id"] == expected_section and row["status"] == "invalid" for row in raw["rows"]
    )


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


@pytest.mark.parametrize(
    "invalid_row",
    ["covered", ["covered"], None],
    ids=["scalar", "list", "null"],
)
def test_cli_rejects_non_mapping_coverage_evidence_rows(
    tmp_path: Path, invalid_row: object
) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, "covered", plan_hash=plan_hash)
    evidence_matrix = tmp_path / "invalid-evidence.coverage.yaml"
    evidence_matrix.write_text(
        yaml.safe_dump({"rows": [invalid_row]}),
        encoding="utf-8",
    )
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(
        cli,
        [
            *_base_args(plan_path, plan_hash, matrix, manifest),
            "--evidence",
            f"coverage-matrix:{evidence_matrix}",
        ],
    )

    assert result.exit_code == 3
    assert "row 1 must be a mapping" in result.output
    assert "Traceback" not in result.output


def test_cli_preserves_root_task_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    args = [
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
    ]
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["header"]["root_task_ref"] == "#12725"

    repeat = CliRunner().invoke(cli, args)

    assert repeat.exit_code == 0


def test_cli_exit_code_stale_matrix(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    stale_matrix = _matrix_file(tmp_path, "covered", plan_hash="old")
    manifest = tmp_path / "out.coverage.yaml"
    stale = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, stale_matrix, manifest))
    assert stale.exit_code == 4


def test_cli_matrix_requires_plan_hash_header(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, "covered", plan_hash=None)
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 4
    assert "header.plan_hash" in result.output
    assert not manifest.exists()


def test_cli_matrix_omitted_plan_item_is_missing(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(tmp_path, "covered", plan_hash=plan_hash)
    matrix_data = yaml.safe_load(matrix.read_text(encoding="utf-8"))
    matrix_data["rows"] = [row for row in matrix_data["rows"] if row["item_id"] == "A1.1"]
    matrix.write_text(yaml.safe_dump(matrix_data), encoding="utf-8")
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 2
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    statuses = {row["item_id"]: row["status"] for row in raw["rows"]}
    assert statuses == {"A1.1": "covered", "A1.2": "missing"}


def test_cli_matrix_nonexistent_item_is_invalid(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(
        tmp_path,
        "covered",
        plan_hash=plan_hash,
        rows=[
            {
                "section_id": "Z9",
                "item_id": "Z9.1",
                "plan_node_hash": "fabricated",
                "status": "covered",
            }
        ],
    )
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 3
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    statuses = {(row["section_id"], row["item_id"]): row["status"] for row in raw["rows"]}
    assert statuses == {
        ("Z9", "Z9.1"): "invalid",
        ("A1", "A1.1"): "missing",
        ("A1", "A1.2"): "missing",
    }


def test_cli_matrix_plan_node_hash_mismatch_is_invalid(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
    matrix = _matrix_file(
        tmp_path,
        "covered",
        plan_hash=plan_hash,
        rows=[
            {
                "section_id": "A1",
                "item_id": "A1.1",
                "plan_node_hash": "stale-node-hash",
                "status": "covered",
            }
        ],
    )
    manifest = tmp_path / "out.coverage.yaml"

    result = CliRunner().invoke(cli, _base_args(plan_path, plan_hash, matrix, manifest))

    assert result.exit_code == 3
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    statuses = {row["item_id"]: row["status"] for row in raw["rows"]}
    assert statuses == {"A1.1": "invalid", "A1.2": "missing"}


def test_cli_exit_code_missing_scope(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
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


def test_cli_exit_code_empty_component(tmp_path: Path) -> None:
    plan_path, plan_hash = _plan_file(tmp_path)
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
