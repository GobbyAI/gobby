"""RTK status reporting through the shipped installer status helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.cli.install_setup_rtk import get_rtk_status
from gobby.storage.hub.protocol import HubDatabase
from tests.cli.test_install_setup_rtk import _ensure_rule, _write_fake_rtk

pytestmark = pytest.mark.unit


def test_get_rtk_status_reports_disabled_when_rule_is_off(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_rule(temp_db, enabled=False)
    monkeypatch.delenv("GOBBY_RTK_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    status = get_rtk_status(temp_db, home=tmp_path)

    assert status.rule_enabled is False
    assert status.health == "disabled"


def test_get_rtk_status_reports_unavailable_when_enabled_without_binary(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_rule(temp_db, enabled=True)
    monkeypatch.delenv("GOBBY_RTK_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    status = get_rtk_status(temp_db, home=tmp_path)

    assert status.rule_enabled is True
    assert status.binary_path is None
    assert status.health == "unavailable"


def test_get_rtk_status_reports_healthy_for_compatible_path_binary(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_rule(temp_db, enabled=True)
    binary = tmp_path / "bin" / "rtk"
    _write_fake_rtk(binary)
    monkeypatch.setenv("GOBBY_RTK_BIN", str(binary))

    status = get_rtk_status(temp_db, home=tmp_path / "home")

    assert status.rule_enabled is True
    assert status.binary_path == binary.resolve()
    assert status.version == "0.45.0"
    assert status.health == "healthy"
    assert status.managed_binary is False
