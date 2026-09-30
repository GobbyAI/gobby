"""Tests for bundled runtime-hook argument resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.mcp_proxy import bundled
from gobby.mcp_proxy.bundled import (
    CHROME_EXECUTABLE_PATH_HOOK,
    resolve_chrome_devtools_executable_path,
    resolve_runtime_stdio_args,
)

pytestmark = pytest.mark.unit

_PLAYWRIGHT_ARGS = ["-y", "@playwright/mcp@latest"]


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _clear_chrome_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var_name in (
        "GOBBY_CHROME_EXECUTABLE_PATH",
        "CHROME_EXECUTABLE_PATH",
        "PUPPETEER_EXECUTABLE_PATH",
    ):
        monkeypatch.delenv(var_name, raising=False)


def _bundled_chromium(home: Path, revision: int) -> Path:
    return _touch(
        home
        / f"Library/Caches/ms-playwright/chromium-{revision}/chrome-mac-arm64"
        / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    )


def test_bundled_playwright_template_uses_chrome_executable_hook() -> None:
    from gobby.mcp_proxy.templates import get_bundled_templates_path, load_template_file

    template = load_template_file(get_bundled_templates_path() / "playwright.yaml")

    assert list(template.args) == _PLAYWRIGHT_ARGS
    assert template.runtime_hook == CHROME_EXECUTABLE_PATH_HOOK


def test_resolve_runtime_stdio_args_injects_bundled_chromium_for_playwright() -> None:
    with patch.object(
        bundled, "resolve_chrome_devtools_executable_path", return_value="/cache/chromium"
    ):
        args = resolve_runtime_stdio_args(CHROME_EXECUTABLE_PATH_HOOK, list(_PLAYWRIGHT_ARGS))

    assert args == [*_PLAYWRIGHT_ARGS, "--executable-path=/cache/chromium"]


def test_resolve_runtime_stdio_args_leaves_hookless_playwright_untouched() -> None:
    assert resolve_runtime_stdio_args(None, list(_PLAYWRIGHT_ARGS)) == _PLAYWRIGHT_ARGS


def test_resolver_finds_playwright_bundled_chromium_in_isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_chrome_env(monkeypatch)
    executable = _bundled_chromium(tmp_path, 1234)

    with (
        patch("gobby.mcp_proxy.bundled.shutil.which", return_value=None),
        patch("gobby.mcp_proxy.bundled.platform.system", return_value="Darwin"),
    ):
        assert resolve_chrome_devtools_executable_path() == str(executable)


def test_resolver_prefers_newest_playwright_bundled_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_chrome_env(monkeypatch)
    older = _bundled_chromium(tmp_path, 1234)
    newer = _bundled_chromium(tmp_path, 1243)

    with (
        patch("gobby.mcp_proxy.bundled.shutil.which", return_value=None),
        patch("gobby.mcp_proxy.bundled.platform.system", return_value="Darwin"),
    ):
        resolved = resolve_chrome_devtools_executable_path()

    assert resolved == str(newer)
    assert older.exists()


def test_resolver_returns_none_without_any_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_chrome_env(monkeypatch)

    with (
        patch("gobby.mcp_proxy.bundled.shutil.which", return_value=None),
        patch("gobby.mcp_proxy.bundled.platform.system", return_value="Darwin"),
    ):
        assert resolve_chrome_devtools_executable_path() is None
