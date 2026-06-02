"""Tests for daemon UI startup decisions."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.config.app import DaemonConfig
from gobby.runner_lifecycle_shutdown import _stop_ui_dev_server_if_needed
from gobby.runner_lifecycle_subsystems import _maybe_start_ui_dev_server

pytestmark = pytest.mark.unit


def _source_web_dir(tmp_path: Path) -> Path:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}")
    return web_dir


def test_effective_dev_starts_vite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    web_dir = _source_web_dir(tmp_path)
    config = DaemonConfig(
        ui={"enabled": True, "mode": "auto", "web_dir": str(web_dir)},
        telemetry={"log_file": str(tmp_path / "logs" / "gobby.log")},
    )
    calls: list[tuple[object, ...]] = []

    def fake_spawn_ui_server(*args: object, **kwargs: object) -> int:
        calls.append((*args, kwargs))
        return 1234

    monkeypatch.setattr("gobby.cli.utils.spawn_ui_server", fake_spawn_ui_server)

    _maybe_start_ui_dev_server(SimpleNamespace(config=config))

    assert calls
    assert calls[0][0:4] == ("localhost", 60889, web_dir, tmp_path / "logs" / "ui.log")
    assert calls[0][4] == {"daemon_port": 60887, "ws_port": 60888}


def test_effective_production_skips_vite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    config = DaemonConfig(ui={"enabled": True, "mode": "auto"})
    calls: list[bool] = []

    def fail_spawn_ui_server(*_args: object, **_kwargs: object) -> int:
        calls.append(True)
        raise AssertionError("spawn_ui_server should not be called")

    monkeypatch.setattr("gobby.cli.utils.spawn_ui_server", fail_spawn_ui_server)

    _maybe_start_ui_dev_server(SimpleNamespace(config=config))

    assert calls == []


def test_effective_dev_stops_vite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    web_dir = _source_web_dir(tmp_path)
    config = DaemonConfig(ui={"enabled": True, "mode": "auto", "web_dir": str(web_dir)})
    calls: list[bool] = []

    def fake_stop_ui_server(*, quiet: bool = False) -> bool:
        calls.append(quiet)
        return True

    monkeypatch.setattr("gobby.cli.utils.stop_ui_server", fake_stop_ui_server)

    _stop_ui_dev_server_if_needed(SimpleNamespace(config=config))

    assert calls == [True]
