"""Corpus manifests match by content identity; the run's root and the JSON layout are not evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from runtime_boundary import EvidenceError
from verify_corpora import compare_manifests


def _manifest(root: str, *, sha: str = "a" * 64, commit: str = "c" * 40) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_commit": commit,
        "root": root,
        "control_exclusions": [],
        "source_exclusions": [],
        "file_count": 1,
        "total_bytes": 3,
        "input_tree_sha256": sha,
        "files": [{"path": "main.py", "type": "file", "mode": "644", "bytes": 3, "sha256": sha}],
    }


def _write(root: Path, manifest: dict[str, Any], *, indent: int | None) -> None:
    path = root / "manifests/corpora/gcode/C0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=indent))


def test_equal_corpus_content_under_different_roots_and_layouts_passes(tmp_path: Path) -> None:
    rerun = tmp_path / "rerun"
    september = tmp_path / "september"
    _write(rerun, _manifest("/runtime/rerun/corpora/gcode/C0"), indent=2)
    _write(september, _manifest("/september/corpora/gcode/C0"), indent=None)
    rerun_text = (rerun / "manifests/corpora/gcode/C0.json").read_text()
    september_text = (september / "manifests/corpora/gcode/C0.json").read_text()
    assert rerun_text != september_text

    compare_manifests(rerun, september, cases={"gcode": ("C0",)})


def test_a_changed_file_hash_fails(tmp_path: Path) -> None:
    rerun = tmp_path / "rerun"
    september = tmp_path / "september"
    _write(rerun, _manifest("/runtime/rerun/corpora/gcode/C0"), indent=2)
    _write(september, _manifest("/september/corpora/gcode/C0", sha="b" * 64), indent=2)

    with pytest.raises(EvidenceError, match="manifest differs from September: gcode/C0"):
        compare_manifests(rerun, september, cases={"gcode": ("C0",)})


def test_a_different_source_commit_fails(tmp_path: Path) -> None:
    rerun = tmp_path / "rerun"
    september = tmp_path / "september"
    _write(rerun, _manifest("/runtime/rerun/corpora/gcode/C0"), indent=2)
    _write(september, _manifest("/september/corpora/gcode/C0", commit="d" * 40), indent=2)

    with pytest.raises(EvidenceError, match="manifest differs from September: gcode/C0"):
        compare_manifests(rerun, september, cases={"gcode": ("C0",)})
