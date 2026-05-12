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
