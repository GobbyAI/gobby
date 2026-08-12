"""Tests for the shared GOBBY_TEST_PROTECT guard semantic."""

from __future__ import annotations

import pytest

from gobby.utils.env import is_test_protect_enabled
from tests.fixtures.postgres import _require_test_database_url

pytestmark = pytest.mark.unit


class TestTestProtectEnabled:
    def test_unset_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        assert is_test_protect_enabled() is False

    @pytest.mark.parametrize(
        "value",
        ["", "0", "false", "no", "off", "  ", "FALSE", "Off", " 0 "],
    )
    def test_explicit_opt_outs_are_disabled(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("GOBBY_TEST_PROTECT", value)

        assert is_test_protect_enabled() is False

    @pytest.mark.parametrize(
        "value",
        ["1", "true", "yes", "TRUE", "on", "2", "anything-unrecognized"],
    )
    def test_any_other_set_value_fails_closed_to_enabled(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("GOBBY_TEST_PROTECT", value)

        assert is_test_protect_enabled() is True


def test_postgres_fixture_fails_closed_for_any_enabled_protect_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "anything-unrecognized")

    with pytest.raises(pytest.fail.Exception, match="DATABASE_URL"):
        _require_test_database_url()
