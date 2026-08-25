"""Eligibility resolver for daemon code-index maintenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.code_index.eligibility import (
    code_index_id_for_root,
    overlay_project_id_for_root,
    resolve_indexed_project,
)

pytestmark = pytest.mark.unit


def _write_project_json(root: Path, project_id: str) -> None:
    marker = root / ".gobby"
    marker.mkdir(parents=True)
    (marker / "project.json").write_text(json.dumps({"id": project_id, "name": "x"}))


def _write_isolation_marker(root: Path, parent_root: Path, parent_id: str) -> None:
    marker = root / ".gobby"
    marker.mkdir(parents=True)
    (marker / "project.json").write_text(
        json.dumps(
            {
                "id": parent_id,
                "parent_project_path": str(parent_root),
                "parent_project_id": parent_id,
            }
        )
    )


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


def test_invalid_utf8_marker_is_identity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    marker = root / ".gobby"
    marker.mkdir(parents=True)
    (marker / "project.json").write_bytes(b"\xff\xfe not utf-8")

    decision = resolve_indexed_project(
        project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        root_path=str(root),
        project_exists=True,
        project_deleted=False,
    )

    assert decision.kind == "identity_mismatch"
    assert decision.root == root


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


def test_overlay_when_unregistered_selector_is_the_roots_derived_id(tmp_path: Path) -> None:
    """A live worktree overlay selector is left alone, not reconciled (#20889)."""
    root = tmp_path / "worktree"
    root.mkdir()

    decision = resolve_indexed_project(
        project_id=code_index_id_for_root(root),
        root_path=str(root),
        project_exists=False,
        project_deleted=False,
    )

    assert decision.kind == "overlay"
    assert decision.root == root


def test_unregistered_when_derived_id_root_is_gone(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    derived = code_index_id_for_root(root)
    root.rmdir()

    decision = resolve_indexed_project(
        project_id=derived,
        root_path=str(root),
        project_exists=False,
        project_deleted=False,
    )

    assert decision.kind == "unregistered"


def test_unregistered_when_derived_id_does_not_match_root(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()

    decision = resolve_indexed_project(
        project_id=code_index_id_for_root(tmp_path / "elsewhere"),
        root_path=str(root),
        project_exists=False,
        project_deleted=False,
    )

    assert decision.kind == "unregistered"


def test_soft_deleted_registered_project_still_reconciles(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    decision = resolve_indexed_project(
        project_id=code_index_id_for_root(root),
        root_path=str(root),
        project_exists=True,
        project_deleted=True,
    )

    assert decision.kind == "unregistered"


def test_overlay_project_id_for_isolation_marker(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    root = tmp_path / "worktree"
    root.mkdir()
    _write_isolation_marker(root, parent, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    assert overlay_project_id_for_root(root) == code_index_id_for_root(root)


def test_overlay_project_id_none_for_plain_project_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project_json(root, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    assert overlay_project_id_for_root(root) is None


def test_overlay_project_id_none_for_self_referential_marker(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_isolation_marker(root, root, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    assert overlay_project_id_for_root(root) is None


def test_code_index_id_matches_the_rust_derivation(tmp_path: Path) -> None:
    """UUID5 in the shared namespace over the canonical path, like gobby-core."""
    import uuid as uuid_module

    root = tmp_path / "repo"
    root.mkdir()
    expected = str(
        uuid_module.uuid5(
            uuid_module.UUID("c0de1de0-0000-4000-8000-000000000000"),
            str(root.resolve()),
        )
    )
    assert code_index_id_for_root(root) == expected
