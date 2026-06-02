"""Tests for effective UI mode resolution."""

from pathlib import Path

import pytest

from gobby.cli.ui_mode import resolve_ui_mode
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def _source_web_dir(tmp_path: Path) -> Path:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}")
    return web_dir


def _dist_web_dir(tmp_path: Path) -> Path:
    web_dir = tmp_path / "web"
    dist_dir = web_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<main>production</main>")
    return web_dir


def test_auto_resolves_to_dev_when_source_web_dir_exists(tmp_path: Path) -> None:
    web_dir = _source_web_dir(tmp_path)
    config = DaemonConfig(ui={"mode": "auto", "web_dir": str(web_dir)})

    resolution = resolve_ui_mode(config)

    assert resolution.configured == "auto"
    assert resolution.effective == "dev"
    assert resolution.display == "auto -> dev"
    assert resolution.source_web_dir == web_dir


def test_auto_resolves_to_production_without_source_web_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_dir = _dist_web_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = DaemonConfig(ui={"mode": "auto", "web_dir": str(web_dir)})

    resolution = resolve_ui_mode(config)

    assert resolution.configured == "auto"
    assert resolution.effective == "production"
    assert resolution.display == "auto -> production"
    assert resolution.source_web_dir is None


def test_explicit_dev_wins_without_source_web_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = DaemonConfig(ui={"mode": "dev", "web_dir": str(tmp_path / "missing")})

    resolution = resolve_ui_mode(config)

    assert resolution.configured == "dev"
    assert resolution.effective == "dev"
    assert resolution.display == "dev"


def test_explicit_production_wins_with_source_web_dir(tmp_path: Path) -> None:
    web_dir = _source_web_dir(tmp_path)
    config = DaemonConfig(ui={"mode": "production", "web_dir": str(web_dir)})

    resolution = resolve_ui_mode(config)

    assert resolution.configured == "production"
    assert resolution.effective == "production"
    assert resolution.display == "production"
    assert resolution.source_web_dir is None
