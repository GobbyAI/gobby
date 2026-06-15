from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

import pytest

from gobby.storage.build_profiles import BuildProfileError, BuildProfileLoader, BuildProfileManager

pytestmark = pytest.mark.unit


def test_bundled_build_profiles_sync_and_resolve(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    result = BuildProfileLoader().sync(temp_db)
    manager = BuildProfileManager(temp_db)

    profile = manager.resolve("default", project_id=sample_project["id"])

    assert result.upserted == 5
    assert profile.name == "default"
    assert profile.source == "installed"
    assert profile.skip_stages == []
    assert profile.delivery_mode == "auto"

    submit = manager.resolve("submit", project_id=sample_project["id"])
    assert submit.delivery_mode == "pull_request"
    assert submit.delivery_target_repo is None


def test_project_profile_accepts_delivery_target_repo(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    profile = manager.create(
        name="submit-upstream",
        display_label="Submit Upstream",
        description="Open PRs against upstream.",
        skip_stages=["merge"],
        isolation="worktree",
        unattended=False,
        delivery_mode="pull_request",
        delivery_target_repo="upstream/app",
        source="project",
        project_id=sample_project["id"],
    )

    assert profile.delivery_mode == "pull_request"
    assert profile.delivery_target_repo == "upstream/app"


def test_project_profile_update_changes_delivery_fields(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    manager.create(
        name="submit-upstream",
        display_label="Submit Upstream",
        description="Open PRs against upstream.",
        skip_stages=["merge"],
        isolation="worktree",
        unattended=False,
        delivery_mode="auto",
        source="project",
        project_id=sample_project["id"],
    )

    profile = manager.update(
        "submit-upstream",
        source="project",
        project_id=sample_project["id"],
        updates={
            "delivery_mode": "pull_request",
            "delivery_target_repo": "upstream/app",
        },
    )

    assert profile.delivery_mode == "pull_request"
    assert profile.delivery_target_repo == "upstream/app"


def test_project_profile_update_clears_delivery_target_repo_with_empty_string(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    manager.create(
        name="submit-upstream",
        display_label="Submit Upstream",
        description="Open PRs against upstream.",
        skip_stages=["merge"],
        isolation="worktree",
        unattended=False,
        delivery_mode="pull_request",
        delivery_target_repo="upstream/app",
        source="project",
        project_id=sample_project["id"],
    )

    profile = manager.update(
        "submit-upstream",
        source="project",
        project_id=sample_project["id"],
        updates={"delivery_target_repo": ""},
    )

    assert profile.delivery_target_repo is None


def test_project_profile_rejects_active_duplicate_before_insert(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    manager.create(
        name="local-fast",
        display_label="Local Fast",
        description="Local only",
        skip_stages=[],
        isolation="worktree",
        unattended=False,
        source="project",
        project_id=sample_project["id"],
    )

    with pytest.raises(BuildProfileError, match="already exists"):
        manager.create(
            name="local-fast",
            display_label="Local Fast Duplicate",
            description="Duplicate",
            skip_stages=[],
            isolation="worktree",
            unattended=False,
            source="project",
            project_id=sample_project["id"],
        )


def test_profile_from_row_logs_malformed_json(
    temp_db: Any, sample_project: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    manager = BuildProfileManager(temp_db)
    profile = manager.create(
        name="local-fast",
        display_label="Local Fast",
        description="Local only",
        skip_stages=[],
        isolation="worktree",
        unattended=False,
        source="project",
        project_id=sample_project["id"],
    )
    row = asdict(profile)
    row["skip_stages_json"] = "{malformed"
    row["tags_json"] = "[]"

    with caplog.at_level(logging.DEBUG):
        reloaded = manager._profile_from_row(row)

    assert reloaded is not None
    assert reloaded.skip_stages == []
    assert "Malformed build profile JSON list" in caplog.text


def test_project_profile_rejects_invalid_delivery_target_repo(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)

    with pytest.raises(BuildProfileError, match="delivery_target_repo"):
        manager.create(
            name="bad-submit",
            display_label="Bad Submit",
            description="Invalid target repo.",
            skip_stages=["merge"],
            isolation="worktree",
            unattended=False,
            delivery_mode="pull_request",
            delivery_target_repo="too/many/parts",
            source="project",
            project_id=sample_project["id"],
        )


def test_project_disabled_profile_blocks_installed_fallback(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    BuildProfileLoader().sync(temp_db)
    manager = BuildProfileManager(temp_db)
    manager.create(
        name="default",
        display_label="Default Override",
        description="Project override",
        skip_stages=[],
        isolation="worktree",
        unattended=False,
        enabled=False,
        source="project",
        project_id=sample_project["id"],
    )

    with pytest.raises(BuildProfileError, match="disabled"):
        manager.resolve("default", project_id=sample_project["id"])


def test_custom_profile_cannot_restore_without_bundled_counterpart(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    manager.create(
        name="local-fast",
        display_label="Local Fast",
        description="Local only",
        skip_stages=["pr", "merge"],
        isolation="none",
        unattended=False,
        source="project",
        project_id=sample_project["id"],
    )

    with pytest.raises(BuildProfileError, match="custom"):
        manager.restore("local-fast", source="project", project_id=sample_project["id"])


def test_bundled_profiles_default_plan_enhancement_rounds_to_zero(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    BuildProfileLoader().sync(temp_db)
    manager = BuildProfileManager(temp_db)

    for name in ("default", "autopilot", "fix", "fix-merge", "submit"):
        profile = manager.resolve(name, project_id=sample_project["id"])
        assert profile.plan_enhancement_rounds == 0


def test_project_profile_round_trips_plan_enhancement_rounds(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    created = manager.create(
        name="enhance",
        display_label="Enhance",
        description="Non-zero plan-enhancement rounds.",
        skip_stages=[],
        isolation="worktree",
        unattended=False,
        plan_enhancement_rounds=2,
        source="project",
        project_id=sample_project["id"],
    )
    assert created.plan_enhancement_rounds == 2

    fetched = manager.get("enhance", source="project", project_id=sample_project["id"])
    assert fetched is not None
    assert fetched.plan_enhancement_rounds == 2

    updated = manager.update(
        "enhance",
        source="project",
        project_id=sample_project["id"],
        updates={"plan_enhancement_rounds": 5},
    )
    assert updated.plan_enhancement_rounds == 5


def test_create_rejects_negative_plan_enhancement_rounds(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    manager = BuildProfileManager(temp_db)
    with pytest.raises(BuildProfileError, match="plan_enhancement_rounds"):
        manager.create(
            name="bad-enhance",
            display_label="Bad",
            description="Negative rounds.",
            skip_stages=[],
            isolation="worktree",
            unattended=False,
            plan_enhancement_rounds=-1,
            source="project",
            project_id=sample_project["id"],
        )


def test_resync_after_plan_enhancement_field_does_not_drift(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    # The new plan_enhancement_rounds column must not make unchanged bundled
    # rows look edited: a second sync should skip every profile.
    first = BuildProfileLoader().sync(temp_db)
    assert first.upserted == 5

    second = BuildProfileLoader().sync(temp_db)
    assert second.upserted == 0
    assert second.skipped == 5

    manager = BuildProfileManager(temp_db)
    default = manager.resolve("default", project_id=sample_project["id"])
    assert default.state == "bundled"


def test_sync_refreshes_legacy_hashed_bundled_row(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    # A bundled row whose stored hash predates the plan_enhancement_rounds
    # column (the legacy payload shape) must refresh on the next sync rather
    # than be misread as a user edit and skipped.
    BuildProfileLoader().sync(temp_db)
    manager = BuildProfileManager(temp_db)
    row = temp_db.fetchone(
        "SELECT * FROM build_profiles WHERE name = %s AND source = 'installed'",
        ("default",),
    )
    assert row is not None
    new_hash = row["bundled_hash"]
    legacy_hash = manager.legacy_row_hash(row)
    assert legacy_hash != new_hash

    temp_db.execute(
        "UPDATE build_profiles SET bundled_hash = %s WHERE id = %s",
        (legacy_hash, row["id"]),
    )

    result = BuildProfileLoader().sync(temp_db)
    assert result.upserted >= 1

    refreshed = temp_db.fetchone(
        "SELECT bundled_hash FROM build_profiles WHERE id = %s",
        (row["id"],),
    )
    assert refreshed is not None
    assert refreshed["bundled_hash"] == new_hash
