from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.integrations.github import GitHubIntegration


@pytest.mark.parametrize(
    ("health", "lazy_connect", "expected"),
    [
        ({}, True, True),
        ({"github": {"state": "pending"}}, True, True),
        ({}, False, False),
        ({"github": {"state": "pending"}}, False, False),
        ({"github": {"state": "disconnected"}}, True, False),
    ],
)
def test_github_availability_handles_lazy_connection_states(
    health: dict[str, object],
    lazy_connect: bool,
    expected: bool,
) -> None:
    manager = SimpleNamespace(
        has_server=lambda _name: True,
        health=health,
        lazy_connect=lazy_connect,
    )

    assert GitHubIntegration(manager).is_available() is expected
