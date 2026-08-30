"""Template-keyed runtime hooks for bundled MCP stdio servers."""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Iterable
from pathlib import Path

CHROME_EXECUTABLE_PATH_HOOK = "chrome_executable_path"


def prefers_offline_npx(command: str | None) -> bool:
    """Return True when an npx launch should prefer the local npm cache."""
    return command == "npx"


def resolve_runtime_stdio_args(runtime_hook: str | None, args: list[str] | None) -> list[str]:
    """Resolve runtime stdio args from the instance's declared runtime hook."""
    runtime_args = list(args or [])
    hook = _RUNTIME_STDIO_HOOKS.get(runtime_hook or "")
    if hook is None:
        return runtime_args
    return hook(runtime_args)


def resolve_chrome_devtools_executable_path() -> str | None:
    """Resolve a Chrome executable path when one is available."""
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
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
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
        glob_patterns.append(".cache/puppeteer-browsers/chrome/*/chrome-win*/chrome.exe")
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


def _inject_chrome_executable_path(args: list[str]) -> list[str]:
    runtime_args = _strip_flag_args(args, "--executable-path")
    executable_path = resolve_chrome_devtools_executable_path()
    if executable_path:
        runtime_args.append(f"--executable-path={executable_path}")
    return runtime_args


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


_RUNTIME_STDIO_HOOKS = {
    CHROME_EXECUTABLE_PATH_HOOK: _inject_chrome_executable_path,
}
