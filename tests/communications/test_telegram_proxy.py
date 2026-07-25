"""Tests for Telegram Bot API proxy configuration."""

from collections.abc import Callable

import pytest

from gobby.communications.telegram_proxy import resolve_telegram_proxy_url


def _resolver(values: dict[str, str]) -> Callable[[str], str | None]:
    return values.get


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:8080",
        "socks5://127.0.0.1:1080",
        "socks5h://127.0.0.1:1080",
    ],
)
def test_resolve_telegram_proxy_url_accepts_supported_direct_proxies(proxy_url: str) -> None:
    assert resolve_telegram_proxy_url(proxy_url, _resolver({})) == proxy_url


def test_resolve_telegram_proxy_url_resolves_authenticated_proxy_secret() -> None:
    proxy_url = "socks5://proxy-user:proxy-password@127.0.0.1:1080"

    assert (
        resolve_telegram_proxy_url(
            "$secret:TELEGRAM_PROXY_URL",
            _resolver({"TELEGRAM_PROXY_URL": proxy_url}),
        )
        == proxy_url
    )


@pytest.mark.parametrize("value", [None, ""])
def test_resolve_telegram_proxy_url_preserves_direct_mode(value: object) -> None:
    assert resolve_telegram_proxy_url(value, _resolver({})) is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (42, "must be a string"),
        ("https://127.0.0.1:8080", "must use http, socks5, or socks5h"),
        ("ftp://127.0.0.1:21", "must use http, socks5, or socks5h"),
        ("http:///missing-host", "must include a host"),
        (
            "http://proxy-user:proxy-password@127.0.0.1:8080",
            r"credentials must use a \$secret: reference",
        ),
        ("$secret:", "must name a secret"),
        ("$secret:MISSING_PROXY", "Could not resolve"),
    ],
)
def test_resolve_telegram_proxy_url_rejects_unsafe_or_invalid_values(
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_telegram_proxy_url(value, _resolver({}))
