"""Isolation marker pin provenance is complete or absent, never partial."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gobby.utils import project_context
from gobby.utils.project_context import (
    ensure_project_json_for_isolation,
    read_isolation_marker,
    validate_isolation_pin_provenance,
)

pytestmark = pytest.mark.unit

PROVENANCE = {
    "base_commit": "a" * 40,
    "ancestor_checkout_path": "/Users/josh/repo",
    "ancestor_checkout_identity": "parent-project",
}


def test_marker_requires_complete_pin_provenance() -> None:
    assert validate_isolation_pin_provenance(dict(PROVENANCE)) == PROVENANCE
    assert validate_isolation_pin_provenance({}) is None
    for key in PROVENANCE:
        partial = {name: value for name, value in PROVENANCE.items() if name != key}
        with pytest.raises(ValueError):
            validate_isolation_pin_provenance(partial)


PARENT_ID = "parent-project"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_marker(root: Path, payload: dict[str, str]) -> None:
    marker = root / ".gobby" / "isolation.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload), encoding="utf-8")


async def test_writer_persists_complete_pin_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    (source / ".gobby").mkdir(parents=True)
    (source / ".gobby" / "project.json").write_text(json.dumps({"id": PARENT_ID}), encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "seed")
    head = _git(source, "rev-parse", "HEAD")
    isolated.mkdir()
    monkeypatch.setattr(project_context, "link_checkout_cargo_target", lambda *args, **kwargs: None)

    await ensure_project_json_for_isolation(source, isolated)

    assert read_isolation_marker(isolated) == {
        "parent_project_path": str(source.resolve()),
        "parent_project_id": PARENT_ID,
        "base_commit": head,
        "ancestor_checkout_path": str(source.resolve()),
        "ancestor_checkout_identity": PARENT_ID,
    }


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", name)


def _seed_source(source: Path) -> str:
    (source / ".gobby").mkdir(parents=True)
    (source / ".gobby" / "project.json").write_text(json.dumps({"id": PARENT_ID}), encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "seed")
    return _git(source, "rev-parse", "HEAD")


async def test_writer_records_selected_base_when_source_head_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    selected = _seed_source(source)
    _commit(source, "later.txt")
    isolated.mkdir()
    monkeypatch.setattr(project_context, "link_checkout_cargo_target", lambda *args, **kwargs: None)

    await ensure_project_json_for_isolation(source, isolated, base_commit=selected)

    marker = read_isolation_marker(isolated)
    assert marker is not None
    assert marker["base_commit"] == selected


async def test_repair_keeps_recorded_base_after_source_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    selected = _seed_source(source)
    isolated.mkdir()
    monkeypatch.setattr(project_context, "link_checkout_cargo_target", lambda *args, **kwargs: None)
    await ensure_project_json_for_isolation(source, isolated, base_commit=selected)
    marker_bytes = (isolated / ".gobby" / "isolation.json").read_bytes()
    _commit(source, "later.txt")

    await ensure_project_json_for_isolation(source, isolated)

    marker = read_isolation_marker(isolated)
    assert marker is not None
    assert marker["base_commit"] == selected
    assert (isolated / ".gobby" / "isolation.json").read_bytes() == marker_bytes


def test_reader_returns_complete_pin_provenance(tmp_path: Path) -> None:
    payload = {
        "parent_project_path": "/Users/josh/repo",
        "parent_project_id": PARENT_ID,
        **PROVENANCE,
    }
    _write_marker(tmp_path, payload)

    assert read_isolation_marker(tmp_path) == payload


def test_reader_rejects_partial_pin_provenance(tmp_path: Path) -> None:
    _write_marker(
        tmp_path,
        {
            "parent_project_path": "/Users/josh/repo",
            "parent_project_id": PARENT_ID,
            "base_commit": "a" * 40,
        },
    )

    assert read_isolation_marker(tmp_path) is None
