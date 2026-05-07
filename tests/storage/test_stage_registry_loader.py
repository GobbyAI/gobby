"""Red tests for the bundled stage-registry loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit

CANONICAL_STAGE_NAMES = [
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "expansion",
    "development",
    "holistic_qa",
    "pr",
    "merge",
]
DROPPED_STAGE_NAMES = {"adversarial_review", "expansion_qa", "code_review_qa"}


def _loader_types():
    from gobby.storage.tasks._stage_registry_loader import (  # noqa: PLC0415
        StageRegistryLoader,
        StageRegistryLoadError,
    )

    return StageRegistryLoader, StageRegistryLoadError


def _fresh_db(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "registry-loader.db")
    run_migrations(db)
    return db


def test_parses_bundled_yaml() -> None:
    StageRegistryLoader, _ = _loader_types()

    entries = StageRegistryLoader().load()
    by_name = {entry.name: entry for entry in entries}

    assert [entry.name for entry in entries] == CANONICAL_STAGE_NAMES
    assert {entry.name for entry in entries}.isdisjoint(DROPPED_STAGE_NAMES)
    assert entries[0].display_label == "Ideation"
    assert entries[0].default_agent == "analyst"
    assert entries[-1].name == "merge"
    assert entries[-1].is_terminal is True
    assert by_name["planning"].review_policy == "required"
    assert by_name["planning"].reviewer_agent == "plan-adversary"
    assert by_name["expansion"].reviewer_agent == "expansion-qa"
    assert by_name["expansion"].dispatch_type == "pipeline"
    assert by_name["expansion"].dispatch_target == "expand-task"
    assert by_name["expansion"].dispatch_inputs_json is not None
    assert by_name["development"].reviewer_agent is None
    assert json.loads(by_name["development"].reviewer_agent_selector_json or "{}") == {
        "default": "qa-reviewer",
        "rules": [{"category": "docs", "reviewer_agent": "doc-reviewer"}],
    }
    assert by_name["pr"].review_policy == "required"
    assert by_name["pr"].reviewer_agent is None
    assert by_name["merge"].review_policy == "none"


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    StageRegistryLoader, StageRegistryLoadError = _loader_types()
    broken = tmp_path / "stages.yaml"
    broken.write_text(
        yaml.safe_dump({"version": 1, "stages": [{"name": "ideation"}]}),
        encoding="utf-8",
    )

    with pytest.raises(StageRegistryLoadError, match="display_label|description|required"):
        StageRegistryLoader(path=broken).load()


def test_invalid_review_policy_raises(tmp_path: Path) -> None:
    StageRegistryLoader, StageRegistryLoadError = _loader_types()
    broken = tmp_path / "stages.yaml"
    broken.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "stages": [
                    {
                        "name": "planning",
                        "display_label": "Planning",
                        "description": "Plan",
                        "category": "design",
                        "review_policy": "always",
                        "position_hint": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StageRegistryLoadError, match="invalid review_policy"):
        StageRegistryLoader(path=broken).load()


def test_required_review_policy_requires_reviewer_agent_except_pr(tmp_path: Path) -> None:
    StageRegistryLoader, StageRegistryLoadError = _loader_types()
    broken = tmp_path / "stages.yaml"
    broken.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "stages": [
                    {
                        "name": "planning",
                        "display_label": "Planning",
                        "description": "Plan",
                        "category": "design",
                        "review_policy": "required",
                        "position_hint": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StageRegistryLoadError, match="reviewer_agent or reviewer_agent_selector"):
        StageRegistryLoader(path=broken).load()


def test_required_review_policy_accepts_reviewer_selector_default(tmp_path: Path) -> None:
    StageRegistryLoader, _ = _loader_types()
    stages = tmp_path / "stages.yaml"
    stages.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "stages": [
                    {
                        "name": "development",
                        "display_label": "Development",
                        "description": "Work",
                        "category": "implementation",
                        "review_policy": "required",
                        "reviewer_agent_selector": {"default": "qa-reviewer"},
                        "position_hint": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    entry = StageRegistryLoader(path=stages).load()[0]

    assert entry.reviewer_agent is None
    assert json.loads(entry.reviewer_agent_selector_json or "{}") == {
        "default": "qa-reviewer",
        "rules": [],
    }


@pytest.mark.parametrize(
    ("selector", "message"),
    [
        (
            {
                "rules": [
                    {
                        "category": "docs",
                        "task_type": "task",
                        "reviewer_agent": "doc-reviewer",
                    }
                ],
            },
            "exactly one of category or task_type",
        ),
        (
            {"rules": [{"category": "invalid", "reviewer_agent": "doc-reviewer"}]},
            "invalid category",
        ),
        (
            {"rules": [{"task_type": "invalid", "reviewer_agent": "doc-reviewer"}]},
            "invalid task_type",
        ),
    ],
)
def test_reviewer_selector_validation_errors(
    tmp_path: Path,
    selector: dict[str, object],
    message: str,
) -> None:
    StageRegistryLoader, StageRegistryLoadError = _loader_types()
    broken = tmp_path / "stages.yaml"
    broken.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "stages": [
                    {
                        "name": "development",
                        "display_label": "Development",
                        "description": "Work",
                        "category": "implementation",
                        "review_policy": "required",
                        "reviewer_agent_selector": selector,
                        "position_hint": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StageRegistryLoadError, match=message):
        StageRegistryLoader(path=broken).load()


def test_sync_no_op_when_hash_matches_seed(tmp_path: Path) -> None:
    StageRegistryLoader, _ = _loader_types()
    db = _fresh_db(tmp_path)
    before = db.fetchall(
        "SELECT name, bundled_hash, updated_at FROM task_stages_registry ORDER BY name"
    )

    result = StageRegistryLoader().sync(db)

    after = db.fetchall(
        "SELECT name, bundled_hash, updated_at FROM task_stages_registry ORDER BY name"
    )
    assert result.upserted == 0
    assert result.skipped == len(CANONICAL_STAGE_NAMES)
    assert [(row["name"], row["bundled_hash"]) for row in after] == [
        (row["name"], row["bundled_hash"]) for row in before
    ]


def test_sync_upserts_on_hash_drift(tmp_path: Path) -> None:
    StageRegistryLoader, _ = _loader_types()
    db = _fresh_db(tmp_path)
    db.execute(
        """
        UPDATE task_stages_registry
        SET bundled_hash = ?, display_label = ?, review_policy = 'none', reviewer_agent = NULL
        WHERE name = ?
        """,
        ("stale-hash", "Old Planning", "planning"),
    )

    result = StageRegistryLoader().sync(db)

    row = db.fetchone(
        """
        SELECT display_label, bundled_hash, review_policy, reviewer_agent,
               reviewer_agent_selector_json, dispatch_type
        FROM task_stages_registry
        WHERE name = 'planning'
        """
    )
    assert result.upserted >= 1
    assert row["display_label"] == "Planning"
    assert row["bundled_hash"] != "stale-hash"
    assert row["review_policy"] == "required"
    assert row["reviewer_agent"] == "plan-adversary"
    assert row["dispatch_type"] is None


def test_user_added_stage_preserved(tmp_path: Path) -> None:
    StageRegistryLoader, _ = _loader_types()
    db = _fresh_db(tmp_path)
    db.execute(
        """
        INSERT INTO task_stages_registry (
            name, display_label, description, category, position_hint, requires_human,
            is_terminal, bundled_hash, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            "operator_review",
            "Operator Review",
            "A local operator-added stage.",
            "verification",
            999,
            1,
            0,
            None,
        ),
    )

    StageRegistryLoader().sync(db)

    assert (
        db.fetchone("SELECT name FROM task_stages_registry WHERE name = 'operator_review'")
        is not None
    )
