"""Bundled external MCP server definitions and normalization helpers."""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.models import MCPServerConfig

CHROME_DEVTOOLS_SERVER_NAME = "chrome-devtools"
CHROME_DEVTOOLS_NPM_PACKAGE = "chrome-devtools-mcp@0.21.0"
PLAYWRIGHT_SERVER_NAME = "playwright"
LEGACY_GLOBAL_PROJECT_IDS = frozenset({"global"})
# Keep this helper module leaf-level to avoid import cycles through gobby.storage.__init__.
GLOBAL_PROJECT_ID = "00000000-0000-0000-0000-000000000002"

DEFAULT_EXTERNAL_MCP_SERVERS: list[dict[str, Any]] = [
    {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "$secret:github_personal_access_token"},
        "description": "GitHub API integration for issues, PRs, repos, and code search",
    },
    {
        "name": "linear",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-linear"],
        "env": {"LINEAR_API_KEY": "$secret:linear_api_key"},
        "description": "Linear issue tracking integration",
    },
    {
        "name": "brave-search",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@brave/brave-search-mcp-server"],
        "env": {"BRAVE_API_KEY": "$secret:brave_api_key"},
        "description": "Brave Search API for web search, local search, and news",
    },
    {
        "name": "context7",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "optional_secret_args": {"context7_api_key": ["--api-key"]},
        "description": (
            "Context7 library documentation lookup (set context7_api_key secret for private repos)"
        ),
    },
    {
        "name": PLAYWRIGHT_SERVER_NAME,
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "description": "Playwright MCP for browser automation and inspection",
    },
    {
        "name": CHROME_DEVTOOLS_SERVER_NAME,
        "transport": "stdio",
        "command": "npx",
        # Pin the package version so local installs and CI use the same DevTools server build.
        "args": ["-y", CHROME_DEVTOOLS_NPM_PACKAGE, "--no-usage-statistics"],
        "description": "Chrome DevTools MCP for browser debugging and automation",
    },
]

BUNDLED_EXTERNAL_MCP_SERVER_NAMES = frozenset(
    server["name"] for server in DEFAULT_EXTERNAL_MCP_SERVERS
)


def is_bundled_external_mcp_server(name: str) -> bool:
    """Return True when a server name is managed by Gobby as a bundled external MCP."""
    return name.lower() in BUNDLED_EXTERNAL_MCP_SERVER_NAMES


def canonical_project_id_for_server(name: str, project_id: str) -> str:
    """Return the canonical project scope for a server."""
    if is_bundled_external_mcp_server(name):
        return GLOBAL_PROJECT_ID
    return project_id


def normalize_persisted_args(name: str, args: list[str] | None) -> list[str] | None:
    """Strip runtime-only arguments before persisting server config."""
    if not args:
        return args

    normalized_name = name.lower()
    normalized_args = list(args)
    if normalized_name == CHROME_DEVTOOLS_SERVER_NAME:
        normalized_args = _strip_flag_args(normalized_args, "--executable-path")

    return normalized_args or None


def normalize_bundled_server_config(config: MCPServerConfig) -> MCPServerConfig:
    """Return a config normalized to Gobby's bundled-server storage rules."""
    project_id = canonical_project_id_for_server(config.name, config.project_id)
    args = normalize_persisted_args(config.name, config.args)
    if project_id == config.project_id and args == config.args:
        return config
    return replace(config, project_id=project_id, args=args)


def resolve_runtime_stdio_args(name: str, args: list[str] | None) -> list[str]:
    """Resolve runtime stdio args, adding host-specific browser paths only at launch time."""
    runtime_args = list(normalize_persisted_args(name, args) or [])
    if name.lower() != CHROME_DEVTOOLS_SERVER_NAME:
        return runtime_args

    if _has_flag(runtime_args, "--executable-path"):
        return runtime_args

    executable_path = resolve_chrome_devtools_executable_path()
    if executable_path:
        runtime_args.append(f"--executable-path={executable_path}")

    return runtime_args


def resolve_chrome_devtools_executable_path() -> str | None:
    """Resolve a Chrome executable path for chrome-devtools-mcp when one is available."""
    env_path = _first_existing_path(
        os.environ.get(var_name)
        for var_name in (
            "GOBBY_CHROME_EXECUTABLE_PATH",
            "CHROME_EXECUTABLE_PATH",
            "PUPPETEER_EXECUTABLE_PATH",
        )
    )
    if env_path:
        return env_path

    which_path = _first_existing_path(
        shutil.which(candidate)
        for candidate in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
            "msedge",
        )
    )
    if which_path:
        return which_path

    system = platform.system()
    path_candidates: list[Path] = []
    glob_patterns: list[str] = []

    if system == "Darwin":
        path_candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home()
                / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                Path(
                    "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
                ),
                Path.home()
                / "Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            ]
        )
        glob_patterns.extend(
            [
                ".cache/puppeteer-browsers/chrome/*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "Library/Caches/puppeteer/chrome/*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            ]
        )
    elif system == "Windows":
        program_files = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        for root in filter(None, program_files):
            path_candidates.extend(
                [
                    Path(root) / "Google/Chrome/Application/chrome.exe",
                    Path(root) / "Google/Chrome for Testing/Application/chrome.exe",
                    Path(root) / "Chromium/Application/chrome.exe",
                    Path(root) / "Microsoft/Edge/Application/msedge.exe",
                ]
            )
        glob_patterns.append(
            ".cache/puppeteer-browsers/chrome/*/chrome-win*/chrome.exe"
        )
    else:
        path_candidates.extend(
            [
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
                Path("/snap/bin/chromium"),
            ]
        )
        glob_patterns.extend(
            [
                ".cache/puppeteer-browsers/chrome/*/chrome-linux*/chrome",
                ".cache/puppeteer/chrome/*/chrome-linux*/chrome",
            ]
        )

    direct_path = _first_existing_path(str(path) for path in path_candidates)
    if direct_path:
        return direct_path

    for pattern in glob_patterns:
        matches = sorted(Path.home().glob(pattern), reverse=True)
        resolved = _first_existing_path(str(match) for match in matches)
        if resolved:
            return resolved

    return None


def _has_flag(args: list[str], flag: str) -> bool:
    """Return True when args already contain a flag or flag=value form."""
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in args)


def _strip_flag_args(args: list[str], flag: str) -> list[str]:
    """Remove --flag value and --flag=value forms from args."""
    stripped: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == flag:
            skip_next = True
            continue
        if arg.startswith(f"{flag}="):
            continue
        stripped.append(arg)
    return stripped


def _first_existing_path(candidates: Iterable[str | None]) -> str | None:
    """Return the first existing file path from an iterable of string candidates."""
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None
