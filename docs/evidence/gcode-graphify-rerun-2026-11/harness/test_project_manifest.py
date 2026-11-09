"""The 54 gcode projects of criterion 2.1.3: their set, manifest, workspaces and registration."""

import json
import shutil
from pathlib import Path

import launch_services
import pytest
from provision_environment import (
    INDEX_CASES,
    PROJECT_MANIFEST,
    materialize_workspaces,
    project_entries,
    write_project_manifest,
)
from runtime_boundary import EvidenceError

SCORED_CASES = ("C1", "C2", "C3", "C4", "C5", "C6")
SINGLE_ROLES = {
    "interrupted": 1,
    "clean": 1,
    "baseline": 1,
    "change": 1,
    "labels-properties": 1,
    "probe": 1,
}


def _sealed_corpora(root: Path) -> None:
    for case in INDEX_CASES:
        corpus = root / "corpora/gcode" / case
        corpus.mkdir(parents=True)
        (corpus / "README.md").write_text(f"{case}\n", encoding="utf-8")


def test_the_criterion_set_is_fifty_four_unique_projects() -> None:
    entries = project_entries()
    roles = [entry["role"] for entry in entries]

    assert len(entries) == 54
    assert len({entry["name"] for entry in entries}) == 54
    assert roles.count("repetition") == 36
    assert roles.count("spare") == 12
    assert {role: roles.count(role) for role in SINGLE_ROLES} == SINGLE_ROLES


def test_each_scored_case_has_six_repetitions_and_two_spares() -> None:
    entries = project_entries()
    for case in SCORED_CASES:
        mine = [entry for entry in entries if entry["case"] == case]
        assert sorted(e["repetition"] for e in mine if e["role"] == "repetition") == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]
        assert [e["role"] for e in mine].count("spare") == 2


def test_every_project_draws_from_a_sealed_case_into_its_own_workspace() -> None:
    entries = project_entries()

    assert {entry["case"] for entry in entries} <= set(INDEX_CASES)
    assert len({entry["workspace"] for entry in entries}) == 54
    for entry in entries:
        assert entry["corpus"] == f"corpora/gcode/{entry['case']}"
        assert entry["workspace"] == f"workspaces/{entry['name']}"


def test_the_manifest_refuses_a_project_whose_sealed_corpus_is_missing(tmp_path: Path) -> None:
    _sealed_corpora(tmp_path)
    shutil.rmtree(tmp_path / "corpora/gcode/C6")

    with pytest.raises(EvidenceError, match="corpora/gcode/C6"):
        write_project_manifest(tmp_path)


def test_the_manifest_lists_all_fifty_four_projects(tmp_path: Path) -> None:
    _sealed_corpora(tmp_path)

    manifest = write_project_manifest(tmp_path)

    recorded = json.loads((tmp_path / PROJECT_MANIFEST).read_text(encoding="utf-8"))
    assert recorded == manifest
    assert manifest["count"] == len(manifest["projects"]) == 54


def test_materialize_copies_each_sealed_corpus_and_refuses_an_existing_workspace(
    tmp_path: Path,
) -> None:
    _sealed_corpora(tmp_path)
    write_project_manifest(tmp_path)

    materialize_workspaces(tmp_path)

    assert (tmp_path / "workspaces/gcode-C1-r1/README.md").read_text(encoding="utf-8") == "C1\n"
    assert (tmp_path / "workspaces/gcode-C0-probe/README.md").read_text(encoding="utf-8") == "C0\n"
    with pytest.raises(EvidenceError, match="refusing to overwrite"):
        materialize_workspaces(tmp_path)


def test_the_launch_registers_exactly_the_manifest_workspaces(tmp_path: Path) -> None:
    _sealed_corpora(tmp_path)
    write_project_manifest(tmp_path)

    workspaces = launch_services._workspaces(tmp_path)

    assert len(workspaces) == 54
    assert workspaces[0] == ("gcode-C1-r1", Path("workspaces/gcode-C1-r1"))


def test_the_launch_refuses_a_manifest_that_differs_from_the_criterion_set(
    tmp_path: Path,
) -> None:
    _sealed_corpora(tmp_path)
    write_project_manifest(tmp_path)
    recorded = json.loads((tmp_path / PROJECT_MANIFEST).read_text(encoding="utf-8"))
    recorded["projects"].pop()
    (tmp_path / PROJECT_MANIFEST).write_text(json.dumps(recorded), encoding="utf-8")

    with pytest.raises(EvidenceError, match="criterion 2.1.3"):
        launch_services._workspaces(tmp_path)
