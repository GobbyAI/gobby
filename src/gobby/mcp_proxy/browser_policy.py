"""Agent-facing restrictions for the bundled loopback browser transports."""

from typing import Any
from urllib.parse import urlsplit

from gobby.mcp_proxy.models import MCPServerConfig

_UNSAFE_TOOLS = frozenset({"browser_run_code", "browser_file_upload", "upload_file"})
_NAVIGATION_TOOLS = frozenset({"browser_navigate", "navigate_page", "new_page"})


def filter_browser_tools(
    config: MCPServerConfig | None, tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if config is None or config.runtime_hook != "loopback_browser":
        return tools
    return [tool for tool in tools if tool.get("name") not in _UNSAFE_TOOLS]


def enforce_browser_call(
    config: MCPServerConfig | None, name: str, arguments: dict[str, Any]
) -> None:
    if config is None or config.runtime_hook != "loopback_browser":
        return
    if name in _UNSAFE_TOOLS:
        raise ValueError("Loopback browser refuses host code and file uploads")
    if name not in _NAVIGATION_TOOLS:
        return
    url = arguments.get("url")
    if (
        url is None
        and name == "navigate_page"
        and arguments.get("type") in {"back", "forward", "reload"}
    ):
        return
    if url == "about:blank":
        return
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        allowed = (
            parsed is not None
            and parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise ValueError("Loopback browser requires an HTTP(S) loopback URL")
