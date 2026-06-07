from __future__ import annotations

import pytest

from gobby.config import DaemonConfig, IndexingConfig

pytestmark = pytest.mark.unit


def test_indexing_config_defaults_to_respecting_gitignore() -> None:
    config = IndexingConfig()

    assert config.respect_gitignore is True


def test_daemon_config_includes_indexing_defaults() -> None:
    config = DaemonConfig()

    assert isinstance(config.indexing, IndexingConfig)
    assert config.indexing.respect_gitignore is True
