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
    with misleading ``relation does not exist`` errors. The test schema authority is this
    checkout's own build, so a failure here means the checkout does not carry that migration.
    """
    latest = installed_schema_version()
    if latest < QUERY_READONLY_MIGRATION:
        pytest.fail(
            f"Checkout gdaemon is schema v{latest}, before migration "
            f"{QUERY_READONLY_MIGRATION} (the gobby_query_readonly role). Rebuild it with "
            "`cargo build -p gobby-daemon`."
        )
