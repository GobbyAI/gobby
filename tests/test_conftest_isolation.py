"""The shared conftest provisions an isolated GOBBY_HOME and never the operator's."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _ensure_isolated_bootstrap

pytestmark = pytest.mark.unit


def test_operator_home_is_never_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon-spawned process inherits GOBBY_HOME=~/.gobby; a sandbox may hide
    its bootstrap, and the conftest must leave that home untouched (#20712)."""
    user_home = tmp_path / "user"
    operator_home = user_home / ".gobby"
    operator_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("GOBBY_HOME", str(operator_home))

    _ensure_isolated_bootstrap()

    assert list(operator_home.iterdir()) == []


def test_isolated_home_is_provisioned_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    isolated = tmp_path / "isolated"
    monkeypatch.setenv("GOBBY_HOME", str(isolated))
    monkeypatch.delenv("GOBBY_POSTGRES_TEST_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    _ensure_isolated_bootstrap()
    bootstrap = isolated / "bootstrap.yaml"
    written = bootstrap.read_text(encoding="utf-8")
    assert f"files_home: {isolated / 'files'}" in written
    assert "database_url" not in written

    bootstrap.write_text("datastore_mode: local\n", encoding="utf-8")
    _ensure_isolated_bootstrap()
    assert bootstrap.read_text(encoding="utf-8") == "datastore_mode: local\n"


def test_blank_gobby_home_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("GOBBY_HOME", "  ")

    _ensure_isolated_bootstrap()

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dsn_env", ["DATABASE_URL", "GOBBY_POSTGRES_TEST_DSN"])
def test_test_dsn_is_not_published_as_a_live_daemon_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dsn_env: str
) -> None:
    from gobby.config.bootstrap import load_bootstrap
    from tests.fixtures.postgres import _live_hub_identity

    isolated = tmp_path / "isolated"
    monkeypatch.setenv("GOBBY_HOME", str(isolated))
    monkeypatch.setenv(dsn_env, "postgresql://gobby_test:gobby_test@localhost:60892/gobby_test")

    _ensure_isolated_bootstrap()

    bootstrap = load_bootstrap()
    assert bootstrap.files_home == str(isolated / "files")
    assert bootstrap.database_url is None
    assert _live_hub_identity() is None
