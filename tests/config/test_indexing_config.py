from __future__ import annotations

import pytest

from gobby.config import DaemonConfig, IndexingConfig

pytestmark = pytest.mark.unit


def test_indexing_config_defaults_to_respecting_gitignore() -> None:
    config = IndexingConfig()

    assert config.respect_gitignore is True
    assert config.extra_excludes == []


def test_indexing_config_accepts_extra_exclude_patterns() -> None:
    config = IndexingConfig.model_validate({"extra_excludes": ["generated", "*.snapshot"]})

    assert config.extra_excludes == ["generated", "*.snapshot"]


@pytest.mark.parametrize(
    "value",
    ["generated", [""], ["nested/generated"], ["nested\\generated"]],
)
def test_indexing_config_rejects_invalid_extra_excludes(value: object) -> None:
    with pytest.raises(ValueError, match="extra_excludes"):
        IndexingConfig.model_validate({"extra_excludes": value})


def test_indexing_config_rejects_extra_fields() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        IndexingConfig.model_validate({"respect_gitignore": True, "unexpected": True})


def test_daemon_config_includes_indexing_defaults() -> None:
    config = DaemonConfig()

    assert isinstance(config.indexing, IndexingConfig)
    assert config.indexing.respect_gitignore is True
    assert config.indexing.extra_excludes == []
