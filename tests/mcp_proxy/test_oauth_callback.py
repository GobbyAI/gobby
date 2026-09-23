"""Exercise the temporary loopback OAuth listener without a real browser."""

import asyncio
from urllib.parse import urlencode

import httpx2
import pytest
from mcp.client.auth import OAuthFlowError

from gobby.mcp_proxy.oauth_callback import OAuthCallback

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_callback_rejects_bad_state_then_accepts_valid_code() -> None:
    opened: list[str] = []

    async def open_browser(url: str) -> None:
        opened.append(url)

    async with OAuthCallback(open_browser) as callback:
        await callback.redirect("https://auth.example/authorize?state=expected")
        assert opened == ["https://auth.example/authorize?state=expected"]
        async with httpx2.AsyncClient() as client:
            invalid = await client.get(callback.redirect_uri + "?state=wrong&code=stolen")
            assert invalid.status_code == 400
            assert callback.result is not None and not callback.result.done()
            duplicate = await client.get(callback.redirect_uri + "?state=expected&code=a&code=b")
            assert duplicate.status_code == 400
            query = urlencode(
                {"state": "expected", "code": "accepted", "iss": "https://auth.example"}
            )
            response = await client.get(callback.redirect_uri + "?" + query)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
        result = await callback.wait()
        assert result.code == "accepted"
        assert result.iss == "https://auth.example"
    assert callback.server is not None and not callback.server.is_serving()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("invalid_client", "OAuth authorization failed: invalid_client"),
        ("vendor_private_error", "OAuth authorization failed"),
        ("access_denied", "OAuth authorization was denied"),
    ],
)
async def test_callback_distinguishes_oauth_error_from_unknown_error_and_user_denial(
    error: str,
    expected: str,
) -> None:
    async def open_browser(_url: str) -> None:
        pass

    async with OAuthCallback(open_browser) as callback:
        await callback.redirect("https://auth.example/authorize?state=expected")
        async with httpx2.AsyncClient() as client:
            response = await client.get(
                callback.redirect_uri + f"?state=expected&error={error}&error_description=secret"
            )
        assert "secret" not in response.text
        assert error not in response.text
        with pytest.raises(OAuthFlowError, match=f"^{expected}$"):
            await callback.wait()


@pytest.mark.asyncio
async def test_cancelled_login_closes_listener_and_pending_callback() -> None:
    async def open_browser(_url: str) -> None:
        pass

    callback = OAuthCallback(open_browser)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05), callback:
            await callback.wait()
    assert callback.server is not None and not callback.server.is_serving()
    assert callback.result is not None and callback.result.cancelled()
