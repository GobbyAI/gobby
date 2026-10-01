"""Fixture database URLs stay off the live hub's loopback port and database."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_LIVE_COORDINATES = re.compile(r"(localhost|127\.0\.0\.1|\[::1\]):60891/gobby\b")
# Files that must spell the live coordinates. Everything else uses tests/fixtures/fake_hub.py.
_LIVE_COORDINATE_ALLOWLIST = {
    "fixtures/test_live_hub_guard.py": "proves the connection guard refuses them",
    "fixtures/test_postgres_safety.py": "proves pytest_configure refuses them as DATABASE_URL",
    "cli/test_postgres_backup.py": "backup and restore resolve the managed container from them",
    "cli/test_hub_backup_rehearsal.py": "tests the managed-container detector on them",
    "cli/hub_backup/test_stores.py": "the Postgres dump resolves the managed container from them",
    "cli/hub_backup/test_cli_hub_backup_cli.py": "hub-backup resolves the managed container from them",
    "cli/installers/test_docker_guard.py": "pg_dump resolves the managed container before its guard",
}


def test_no_test_fixture_supplies_the_live_hub_coordinates() -> None:
    """Only allow-listed files spell the live coordinates, and each still needs to.

    The scan is textual, so a URL assembled at runtime escapes it; the
    connection guard in tests/fixtures/postgres.py is the backstop for those.
    """
    hits = {
        path.relative_to(_TESTS_ROOT).as_posix()
        for path in _TESTS_ROOT.rglob("*.py")
        if _LIVE_COORDINATES.search(path.read_text(encoding="utf-8"))
    }

    assert sorted(hits - _LIVE_COORDINATE_ALLOWLIST.keys()) == []
    assert sorted(_LIVE_COORDINATE_ALLOWLIST.keys() - hits) == []
