"""Tests for ClaudePluginsProvider downloads."""

from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from gobby.skills.hubs import claude_plugins
from gobby.skills.hubs.claude_plugins import ClaudePluginsProvider
from gobby.skills.limits import HUB_STREAM_CHUNK_BYTES

pytestmark = pytest.mark.unit


class _OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunk_count = claude_plugins._MAX_RAW_FILE_BYTES // HUB_STREAM_CHUNK_BYTES + 1
        for _ in range(chunk_count):
            yield b"x" * HUB_STREAM_CHUNK_BYTES


def _provider(raw_url: object) -> ClaudePluginsProvider:
    provider = ClaudePluginsProvider(
        hub_name="claude-plugins",
        base_url="https://claude-plugins.dev",
    )
    provider._make_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "skills": [
                {
                    "name": "example-skill",
                    "metadata": {"rawFileUrl": raw_url},
                }
            ]
        }
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_url", "expected_error"),
    [
        ("http://raw.githubusercontent.com/acme/repo/main/SKILL.md", "must use HTTPS"),
        ("https://example.com/acme/repo/main/SKILL.md", "host is not allowed"),
        ("https://169.254.169.254/latest/meta-data", "non-public IP"),
    ],
)
async def test_download_skill_rejects_unsafe_raw_url(
    tmp_path: Path,
    raw_url: str,
    expected_error: str,
) -> None:
    result = await _provider(raw_url).download_skill(
        "example-skill",
        target_dir=str(tmp_path),
    )

    assert result.success is False
    assert expected_error in (result.error or "")


@pytest.mark.asyncio
async def test_download_skill_rejects_redirect_to_private_ip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "raw.githubusercontent.com"
        return httpx.Response(
            302,
            headers={"location": "https://169.254.169.254/latest/meta-data"},
        )

    transport = httpx.MockTransport(handler)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        claude_plugins.httpx,
        "AsyncClient",
        partial(client_type, transport=transport),
    )

    result = await _provider(
        "https://raw.githubusercontent.com/acme/repo/main/SKILL.md"
    ).download_skill("example-skill", target_dir=str(tmp_path))

    assert result.success is False
    assert "non-public IP" in (result.error or "")


@pytest.mark.asyncio
async def test_download_skill_rejects_body_over_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            stream=_OversizedStream(),
            request=request,
        )
    )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        claude_plugins.httpx,
        "AsyncClient",
        partial(client_type, transport=transport),
    )

    result = await _provider(
        "https://raw.githubusercontent.com/acme/repo/main/SKILL.md"
    ).download_skill("example-skill", target_dir=str(tmp_path))

    assert result.success is False
    assert "exceeds size limit" in (result.error or "")


@pytest.mark.asyncio
async def test_download_skill_streams_allowed_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"# Example\n", request=request)
    )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        claude_plugins.httpx,
        "AsyncClient",
        partial(client_type, transport=transport),
    )
    download_dir = tmp_path / "download"
    monkeypatch.setattr(
        claude_plugins.tempfile,
        "mkdtemp",
        lambda prefix: str(download_dir),
    )

    result = await _provider(
        "https://raw.githubusercontent.com/acme/repo/main/SKILL.md"
    ).download_skill("example-skill")

    assert result.success is True
    assert result.is_temp is True
    assert (download_dir / "SKILL.md").read_text() == "# Example\n"
