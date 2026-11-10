"""Write-time validation of agent_build.build_root: a bad root never saves."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, ConfigValidationError
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

BUILD_ROOT_KEY = "agent_build.build_root"


def test_a_relative_build_root_is_refused_and_nothing_is_stored(temp_db: HubDatabase) -> None:
    mutations = ConfigMutations(temp_db)

    with pytest.raises(ConfigValidationError, match="must be absolute") as captured:
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={BUILD_ROOT_KEY: "relative/build-root"}),
        )

    assert captured.value.key == BUILD_ROOT_KEY
    assert ConfigRepository(temp_db).read(resolve_secrets=False).overrides == {}


def test_a_missing_build_root_is_refused(temp_db: HubDatabase, tmp_path: Path) -> None:
    mutations = ConfigMutations(temp_db)
    missing = (tmp_path / "unmounted-volume").resolve(strict=False)

    with pytest.raises(ConfigValidationError, match="does not exist"):
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={BUILD_ROOT_KEY: str(missing)}),
        )

    assert BUILD_ROOT_KEY not in ConfigRepository(temp_db).read(resolve_secrets=False).overrides


def test_an_existing_absolute_build_root_is_stored(temp_db: HubDatabase, tmp_path: Path) -> None:
    root = tmp_path / "builds"
    root.mkdir()
    canonical = str(root.resolve())
    mutations = ConfigMutations(temp_db)

    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={BUILD_ROOT_KEY: canonical}),
    )

    assert ConfigRepository(temp_db).read(resolve_secrets=False).overrides[BUILD_ROOT_KEY] == (
        canonical
    )
