"""Fetched GitHub metadata and raw web README content are untrusted."""

import logging

import httpx
import pytest

from gobby.mcp_proxy.importer import MCPServerImporter

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_github_fetch_paths_clean_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    importer = MCPServerImporter.__new__(MCPServerImporter)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/readme"):
            return httpx.Response(200, text="README\u202e\U000e0061\x00\n")
        return httpx.Response(200, json={"description": "description\u200b", "items": ["\u2066"]})

    with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            metadata = await importer._fetch_github_json(client, "/repos/owner/repo")
            readme = await importer._fetch_github_readme(client, "owner", "repo")
    assert metadata == {"description": "description", "items": [""]}
    assert readme == "README\n"
    assert "GitHub metadata" in caplog.text
    assert "GitHub README" in caplog.text
    assert "tags=1" in caplog.text
    assert "README\u202e" not in caplog.text
