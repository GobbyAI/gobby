"""Short socket pathnames backed by each test's ordinary temporary directory."""

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def short_socket_dir(tmp_path: Path) -> Iterator[Path]:
    """Keep AF_UNIX paths short while retaining pytest's state and leak discovery."""
    with tempfile.TemporaryDirectory(prefix="h") as directory:
        alias = Path(directory) / "s"
        alias.symlink_to(tmp_path, target_is_directory=True)
        yield alias
