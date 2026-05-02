"""Legacy projected-status SQL helpers are deleted."""

from __future__ import annotations

import importlib.util

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_state_sql_module_does_not_exist() -> None:
    assert importlib.util.find_spec("gobby.storage.tasks._state_sql") is None


def test_no_imports_of_canonical_status_case_remain() -> None:
    assert "canonical_status_case" not in source_texts(("src/gobby",))


def test_no_imports_of_is_ready_sql_remain() -> None:
    assert "is_ready_sql" not in source_texts(("src/gobby",))


def test_no_imports_of_status_filter_sql_remain() -> None:
    assert "status_filter_sql" not in source_texts(("src/gobby",))
