"""Shared fixtures for workflow tests."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase
    from gobby.workflows.loader import WorkflowLoader


@pytest.fixture(scope="module")
def _workflow_tmp_dir() -> Iterator[Path]:
    """Module-scoped temp directory for the workflow DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(scope="module")
def workflow_db(_workflow_tmp_dir: Path) -> Iterator[LocalDatabase]:
    """Populate a module-scoped DB with bundled workflows and return it.

    Shared across all tests in a module to avoid expensive repeated syncs.
    Tests using this fixture MUST NOT mutate the database.
    """
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations
    from gobby.workflows.sync import sync_bundled_workflows

    db_path = _workflow_tmp_dir / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    sync_bundled_workflows(db)
    yield db
    db.close()


@pytest.fixture(scope="module")
def db_loader(workflow_db: LocalDatabase) -> WorkflowLoader:
    """Return a WorkflowLoader backed by a DB with bundled workflows."""
    from gobby.workflows.loader import WorkflowLoader

    return WorkflowLoader(db=workflow_db)
