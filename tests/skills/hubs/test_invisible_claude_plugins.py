"""Claude Plugins API metadata is sanitized before entering hub results."""

import logging
from unittest.mock import patch

import httpx
import pytest

from gobby.skills.hubs.claude_plugins import ClaudePluginsProvider

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "list", "details"])
async def test_hub_metadata_is_cleaned(operation: str, caplog: pytest.LogCaptureFixture) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"skills": [{"name": "sample\u202e", "description": "help\U000e0061"}]},
        )

    provider = ClaudePluginsProvider("plugins", "https://claude-plugins.dev")
    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    with patch("gobby.skills.hubs.claude_plugins.httpx.AsyncClient", return_value=client):
        with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
            if operation == "search":
                results = await provider.search("sample")
                assert len(results) == 1
                skill = results[0]
            elif operation == "list":
                results = await provider.list_skills()
                assert len(results) == 1
                skill = results[0]
            else:
                details = await provider.get_skill_details("sample")
                assert details is not None
                skill = details
    assert skill.slug == "sample"
    assert skill.display_name == "sample"
    assert skill.description == "help"
    assert "bidi=1" in caplog.text
    assert "tags=1" in caplog.text
    assert "help" not in caplog.text
