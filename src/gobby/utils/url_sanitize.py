"""Safe URL rendering for logs and diagnostics."""

from urllib.parse import urlsplit


def sanitize_url(url: str) -> str:
    """Strip credentials, query parameters, and fragments from a URL."""
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
    return parsed._replace(netloc=netloc, query="", fragment="").geturl()
