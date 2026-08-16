"""Eligibility resolver for daemon code-index maintenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.code_index.eligibility import resolve_indexed_project

pytestmark = pytest.mark.unit


def _write_project_json(root: Path, project_id: str) -> None:
    marker = root / ".gobby"
    marker.mkdir(parents=True)
    (marker / "project.json").write_text(json.dumps({"id": project_id, "name": "x"}))


def test_active_when_registry_root_and_marker_agree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project_json(root, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    decision = resolve_indexed_project(
        project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        root_path=str(root),
        project_exists=True,
        project_deleted=False,
    )

    assert decision.kind == "active"
    assert decision.root == root


def test_unregistered_when_project_missing() -> None:
    decision = resolve_indexed_project(
        project_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        root_path="/tmp/missing-on-purpose",
        project_exists=False,
        project_deleted=False,
    )
    assert decision.kind == "unregistered"
    assert decision.root is None


def test_missing_root_when_directory_absent(tmp_path: Path) -> None:
    decision = resolve_indexed_project(
        project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        root_path=str(tmp_path / "gone"),
        project_exists=True,
        project_deleted=False,
    )
    assert decision.kind == "missing_root"


def test_identity_mismatch_for_unregistered_vault(tmp_path: Path) -> None:
    vault = tmp_path / "wiki"
    vault.mkdir()

    decision = resolve_indexed_project(
        project_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        root_path=str(vault),
        project_exists=True,
        project_deleted=False,
    )
    assert decision.kind == "identity_mismatch"
    assert (tmp_path / "wiki").is_dir()


def test_identity_mismatch_when_marker_id_differs(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    _write_project_json(root, "dddddddd-dddd-4ddd-8ddd-dddddddddddd")

    decision = resolve_indexed_project(
        project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        root_path=str(root),
        project_exists=True,
        project_deleted=False,
    )
    assert decision.kind == "identity_mismatch"
