from __future__ import annotations

import pytest

from gobby.storage.build_profiles import BuildProfileError, BuildProfileLoader, BuildProfileManager

pytestmark = pytest.mark.unit


def test_bundled_build_profiles_sync_and_resolve(temp_db, sample_project) -> None:
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


def test_project_profile_accepts_delivery_target_repo(temp_db, sample_project) -> None:
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


def test_project_profile_update_changes_delivery_fields(temp_db, sample_project) -> None:
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


def test_project_profile_rejects_invalid_delivery_target_repo(temp_db, sample_project) -> None:
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


def test_project_disabled_profile_blocks_installed_fallback(temp_db, sample_project) -> None:
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


def test_custom_profile_cannot_restore_without_bundled_counterpart(temp_db, sample_project) -> None:
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
