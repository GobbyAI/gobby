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
    from gobby.mcp_proxy.models import MCPServerConfig
    from gobby.storage.projects import GLOBAL_PROJECT_ID

    config = MCPServerConfig(
        name="github",
        project_id=GLOBAL_PROJECT_ID,
        url="https://github.example.test",
        id="github",
    )
    manager = SimpleNamespace(
        has_server=lambda _name: True,
        health=health,
        lazy_connect=lazy_connect,
        server_configs=[config],
        get_server_config=lambda sid: config if sid == "github" else None,
        project_id=None,
    )

    assert GitHubIntegration(manager).is_available() is expected
