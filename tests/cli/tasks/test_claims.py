from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.cli.tasks._utils.claims import get_claimed_task_owners

pytestmark = pytest.mark.unit


def test_claimed_task_owners_degrades_on_psycopg_failure() -> None:
    database = MagicMock()
    database.fetchall.side_effect = psycopg.OperationalError("database unavailable")

    assert get_claimed_task_owners(database) == {}
