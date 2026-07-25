"""Telegram Bot API proxy configuration."""

from collections.abc import Callable
from urllib.parse import urlsplit

_SUPPORTED_PROXY_SCHEMES = frozenset({"http", "socks5", "socks5h"})


def resolve_telegram_proxy_url(
    value: object,
    secret_resolver: Callable[[str], str | None],
) -> str | None:
    """Validate and resolve an optional HTTP or SOCKS proxy URL."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Telegram proxy_url must be a string")

    from_secret = value.startswith("$secret:")
    if from_secret:
        secret_name = value.removeprefix("$secret:")
        if not secret_name:
            raise ValueError("Telegram proxy_url $secret: reference must name a secret")
        resolved = secret_resolver(secret_name)
        if not resolved:
            raise ValueError("Could not resolve Telegram proxy_url")
        proxy_url = resolved.strip()
    else:
        proxy_url = value.strip()

    try:
        parsed = urlsplit(proxy_url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise ValueError("Telegram proxy_url is invalid") from exc

    if scheme not in _SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Telegram proxy_url must use http, socks5, or socks5h")
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("Telegram proxy_url must include a host")
    if not from_secret and (username is not None or password is not None):
        raise ValueError("Telegram proxy_url credentials must use a $secret: reference")
    return proxy_url
