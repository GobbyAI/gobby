"""Read-only corpus checks: recorded files must match byte for byte; extras are reported."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from provision_environment import build_manifest
from runtime_boundary import EvidenceError, write_once
from verify_corpora import compare_case, compare_manifests, verify_corpora


def _case(root: Path) -> Path:
    case = root / "case"
    write_once(case / "src/app.py", "print('one')\n", 0o644)
    write_once(case / "README.md", "# case\n", 0o644)
    return case


def _manifest(case: Path) -> dict[str, object]:
    return build_manifest(case, "a" * 40, [])


def test_unchanged_case_reports_nothing(tmp_path: Path) -> None:
    case = _case(tmp_path)
    assert compare_case(case, _manifest(case)) == {"missing": [], "changed": [], "extra": []}


def test_changed_content_is_reported(tmp_path: Path) -> None:
    case = _case(tmp_path)
    manifest = _manifest(case)
    (case / "src/app.py").write_text("print('two')\n")
    report = compare_case(case, manifest)
    assert report["changed"] == ["src/app.py"]


def test_changed_mode_is_reported(tmp_path: Path) -> None:
    case = _case(tmp_path)
    manifest = _manifest(case)
    os.chmod(case / "README.md", 0o755)
    assert compare_case(case, manifest)["changed"] == ["README.md"]


def test_missing_and_extra_files_are_reported(tmp_path: Path) -> None:
    case = _case(tmp_path)
    manifest = _manifest(case)
    (case / "README.md").unlink()
    write_once(case / "notes.txt", "added\n", 0o644)
    report = compare_case(case, manifest)
    assert report == {"missing": ["README.md"], "changed": [], "extra": ["notes.txt"]}


def test_verify_corpora_reads_each_case_and_writes_nothing(tmp_path: Path) -> None:
    case = _case(tmp_path / "corpora" / "gcode")
    cases = {"gcode": ("case",)}
    manifest_path = tmp_path / "manifests" / "corpora" / "gcode" / "case.json"
    write_once(manifest_path, json.dumps(_manifest(case)), 0o600)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    report = verify_corpora(tmp_path, cases=cases)
    assert report == {"gcode/case": {"missing": [], "changed": [], "extra": []}}
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_manifests_must_match_the_september_copies(tmp_path: Path) -> None:
    rerun = tmp_path / "rerun"
    september = tmp_path / "september"
    write_once(rerun / "manifests/corpora/gcode/C0.json", '{"a": 1}', 0o600)
    write_once(september / "manifests/corpora/gcode/C0.json", '{"a": 2}', 0o600)
    with pytest.raises(EvidenceError, match="gcode/C0"):
        compare_manifests(rerun, september, cases={"gcode": ("C0",)})
