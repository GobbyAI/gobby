"""Tests for SQL string helpers."""

import pytest

from gobby.utils.sql import sql_placeholders

pytestmark = pytest.mark.unit


def test_sql_placeholders_defaults_to_compact_commas() -> None:
    assert sql_placeholders(3) == "%s,%s,%s"


def test_sql_placeholders_supports_custom_separator() -> None:
    assert sql_placeholders(2, separator=", ") == "%s, %s"


def test_sql_placeholders_rejects_empty_lists() -> None:
    with pytest.raises(ValueError, match="count must be greater than or equal to 1"):
        sql_placeholders(0)
