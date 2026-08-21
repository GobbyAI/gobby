"""Shared validation for operator-supplied endpoint URLs."""

from collections.abc import Collection
from urllib.parse import urlsplit

HTTP_URL_SCHEMES = frozenset({"http", "https"})
WEBSOCKET_URL_SCHEMES = frozenset({"ws", "wss"})


def validate_endpoint_url(
    value: str,
    *,
    field_name: str = "URL",
    allowed_schemes: Collection[str] = HTTP_URL_SCHEMES,
) -> str:
    """Require an endpoint URL with an allowed scheme and explicit host."""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")

    parsed = urlsplit(normalized)
    schemes = frozenset(scheme.lower() for scheme in allowed_schemes)
    if parsed.scheme.lower() not in schemes:
        expected = "/".join(sorted(schemes))
        raise ValueError(f"{field_name} must use {expected}")
    if parsed.hostname is None or any(character.isspace() for character in parsed.netloc):
        raise ValueError(f"{field_name} must include a valid host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not embed credentials; use api_key or $secret:NAME")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid port") from exc
    return normalized


def validate_optional_endpoint_url(
    value: str | None,
    *,
    field_name: str = "URL",
    allowed_schemes: Collection[str] = HTTP_URL_SCHEMES,
) -> str | None:
    """Validate an optional endpoint URL while preserving an unset value."""
    if value is None:
        return None
    return validate_endpoint_url(
        value,
        field_name=field_name,
        allowed_schemes=allowed_schemes,
    )
