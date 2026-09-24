"""Catalog routing identities and reference paths fail closed."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit


@pytest.fixture
def catalog_data(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "references" / "tasks"
    root.mkdir(parents=True)
    (root / "overview.md").write_text("# Overview", encoding="utf-8")
    (root / "closing.md").write_text("# Closing", encoding="utf-8")
    return {
        "version": 1,
        "capabilities": [
            {
                "name": "tasks",
                "description": "Manage task work",
                "when": "Working on a task",
                "overview": "references/tasks/overview.md",
                "topics": [
                    {
                        "name": "closing",
                        "description": "Close verified work",
                        "path": "references/tasks/closing.md",
                        "when": "Closing a task",
                    }
                ],
            }
        ],
    }


def _write_catalog(root: Path, data: dict[str, Any]) -> None:
    (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")


def test_catalog_exposes_metadata_without_instruction_bodies(
    tmp_path: Path, catalog_data: dict[str, Any]
) -> None:
    _write_catalog(tmp_path, catalog_data)
    catalog = load_capability_catalog(tmp_path)
    assert catalog.version == 1
    capability = catalog.capabilities[0]
    assert (capability.name, capability.description, capability.when) == (
        "tasks",
        "Manage task work",
        "Working on a task",
    )
    assert capability.overview == "references/tasks/overview.md"
    topic = capability.topics[0]
    assert (topic.name, topic.description, topic.when, topic.path) == (
        "closing",
        "Close verified work",
        "Closing a task",
        "references/tasks/closing.md",
    )
    assert "# Closing" not in catalog.model_dump_json()


@pytest.mark.parametrize(
    "defect,diagnostic",
    [
        ("capability", "Duplicate capability name: tasks"),
        ("topic", "Duplicate topic identifier: tasks/closing"),
        ("identity", "duplicate reference identity"),
        ("missing", "missing reference file"),
        ("traversal", "Invalid reference path"),
        ("absolute", "Invalid reference path"),
        ("symlink", "reference escapes skill directory"),
    ],
)
def test_reference_contract_1_3_2(
    tmp_path: Path, catalog_data: dict[str, Any], defect: str, diagnostic: str
) -> None:
    capability = catalog_data["capabilities"][0]
    topic = capability["topics"][0]
    if defect == "capability":
        catalog_data["capabilities"].append(copy.deepcopy(capability))
    elif defect == "topic":
        capability["topics"].append(copy.deepcopy(topic))
    elif defect == "identity":
        topic["path"] = capability["overview"]
    elif defect == "missing":
        topic["path"] = "references/tasks/missing.md"
    elif defect == "traversal":
        topic["path"] = "references/../secret.md"
    elif defect == "absolute":
        topic["path"] = str(tmp_path / topic["path"])
    elif defect == "symlink":
        (tmp_path / topic["path"]).unlink()
        (tmp_path / topic["path"]).symlink_to(tmp_path.parent / "outside.md")
    _write_catalog(tmp_path, catalog_data)
    with pytest.raises(ValueError, match=diagnostic):
        load_capability_catalog(tmp_path)


@pytest.mark.parametrize("version", [True, "1", 2, None])
def test_catalog_requires_supported_integer_version(
    tmp_path: Path, catalog_data: dict[str, Any], version: object
) -> None:
    catalog_data["version"] = version
    _write_catalog(tmp_path, catalog_data)
    with pytest.raises(ValueError, match="expected integer 1"):
        load_capability_catalog(tmp_path)


def test_catalog_rejects_instruction_bodies(tmp_path: Path, catalog_data: dict[str, Any]) -> None:
    catalog_data["capabilities"][0]["content"] = "Instructions belong in reference files"
    _write_catalog(tmp_path, catalog_data)
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_capability_catalog(tmp_path)


@pytest.mark.parametrize(
    "mapping",
    [
        {"tasks": "gobby:references/tasks/missing.md"},
        {"tasks": "tasks"},
        {"gobby:references/tasks/overview.md": "gobby:references/tasks/overview.md"},
    ],
)
def test_catalog_rejects_invalid_folded_skill_mapping(
    tmp_path: Path, catalog_data: dict[str, Any], mapping: dict[str, str]
) -> None:
    catalog_data["folded_skills"] = mapping
    _write_catalog(tmp_path, catalog_data)
    with pytest.raises(ValueError, match="Folded skill"):
        load_capability_catalog(tmp_path)


def test_catalog_rejects_duplicate_folded_names(tmp_path: Path) -> None:
    (tmp_path / "catalog.json").write_text(
        '{"version": 1, "capabilities": [], "folded_skills": {"tasks": "a", "tasks": "b"}}'
    )
    with pytest.raises(ValueError, match="Duplicate catalog key: tasks"):
        load_capability_catalog(tmp_path)


def test_bundled_catalog_maps_all_twenty_nine_folded_skills() -> None:
    catalog = load_capability_catalog()
    assert len(catalog.folded_skills) == 29
    assert "review-learning" not in catalog.folded_skills
    assert catalog.folded_skills["live-session"] == "gobby:references/tasks/live-work.md"
    assert "brevity" not in catalog.folded_skills
    assert "gusto" not in catalog.folded_skills


def test_bundled_catalog_is_valid() -> None:
    assert load_capability_catalog().version == 1
