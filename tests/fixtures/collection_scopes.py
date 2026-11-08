"""Preserve conftest scope across interleaved explicit-file arguments.

Pytest 9.1.1 recreates directory collectors and orphans their fixtures when
argument paths revisit a parent (upstream pytest-dev/pytest#14635).
"""

from collections.abc import Generator
from pathlib import Path

import pytest

_DIRECTORIES = pytest.StashKey[dict[Path, pytest.Directory]]()


@pytest.hookimpl(wrapper=True)
def pytest_collect_directory(
    path: Path, parent: pytest.Collector
) -> Generator[None, pytest.Collector | None, pytest.Collector | None]:
    collector = yield
    if isinstance(collector, pytest.Directory):
        directories = parent.config.stash.setdefault(_DIRECTORIES, {})
        return directories.setdefault(path, collector)
    return collector
