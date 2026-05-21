"""Task search must use stage-native filtering."""

from __future__ import annotations

import inspect

import pytest

from gobby.storage.tasks import _search
from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_search_does_not_import_state_sql() -> None:
    assert "_state_sql" not in source_text("src/gobby/storage/tasks/_search.py")


def test_status_filter_kwarg_removed_or_replaced_with_stage_state() -> None:
    signature = inspect.signature(_search.search_tasks)

    assert "status" not in signature.parameters
    assert "current_stage_state" in signature.parameters


def test_postgres_keyword_pushdown_preserved() -> None:
    source = source_text("src/gobby/storage/tasks/_search.py")

    assert "pdb.score(t.id)" in source
    assert "@@@" in source
    assert "tasks_fts" not in source
    assert " MATCH " not in source
