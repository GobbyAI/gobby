"""Shared fixtures for the terminal-driven session tools."""

from __future__ import annotations

import pytest

from gobby.storage.schema_contract import installed_schema_version

QUERY_READONLY_MIGRATION = 482


@pytest.fixture(autouse=True)
def _no_enter_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap before the submitting Enter is live-CLI timing, not something to wait on."""
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)


@pytest.fixture
def query_readonly_gdaemon() -> None:
    """Refuse to run the query_readonly suites on a gdaemon that predates migration 482.

    The role and its column grants come from that migration, so a pre-482 binary fails
    with misleading ``relation does not exist`` errors. Opt in to the checkout build with
    ``GOBBY_TEST_GDAEMON=checkout`` after ``cargo build -p gobby-daemon``.
    """
    latest = installed_schema_version()
    if latest < QUERY_READONLY_MIGRATION:
        pytest.fail(
            f"Selected gdaemon is schema v{latest}, before migration "
            f"{QUERY_READONLY_MIGRATION} (the gobby_query_readonly role). Run "
            "`cargo build -p gobby-daemon` and set GOBBY_TEST_GDAEMON=checkout."
        )
