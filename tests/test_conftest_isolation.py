"""The shared conftest provisions an isolated GOBBY_HOME and never the operator's."""

from __future__ import annotations

import os
from collections.abc import Callable
from inspect import unwrap
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from tests.conftest import _clear_invoking_agent_identity, _ensure_isolated_bootstrap

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("inherited_home", ["operator", "missing", "blank", "isolated"])
def test_configuration_isolates_bootstrap_before_postgres_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited_home: str
) -> None:
    from tests import conftest as shared_conftest
    from tests.fixtures import postgres

    user_home = tmp_path / "user"
    operator_home = user_home / ".gobby"
    operator_home.mkdir(parents=True)
    sentinel = operator_home / "bootstrap.yaml"
    sentinel.write_text("operator sentinel\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    if inherited_home == "missing":
        monkeypatch.delenv("GOBBY_HOME", raising=False)
    elif inherited_home == "isolated":
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "isolated"))
    else:
        monkeypatch.setenv(
            "GOBBY_HOME", str(operator_home) if inherited_home == "operator" else " "
        )
    initial_home = os.environ.get("GOBBY_HOME")

    observed: list[Path] = []
    original_stat = Path.stat

    def audited_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        assert path != sentinel, "operator bootstrap metadata was accessed"
        if path.name == "bootstrap.yaml":
            observed.append(path)
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", audited_stat)
    cleanups: list[Callable[[], None]] = []
    config = MagicMock(spec=pytest.Config)
    config.add_cleanup.side_effect = cleanups.append
    manager = pytest.PytestPluginManager()
    # Register the probe last so it would run first without tryfirst isolation.
    manager.register(SimpleNamespace(pytest_configure=shared_conftest.pytest_configure))
    manager.register(SimpleNamespace(pytest_configure=postgres.pytest_configure))
    try:
        manager.hook.pytest_configure.call_historic(kwargs={"config": config})
        selected = Path(os.environ["GOBBY_HOME"])
        assert selected != operator_home
        if inherited_home == "isolated":
            assert selected == tmp_path / "isolated"
            assert cleanups == []
        assert selected / "bootstrap.yaml" in observed
        assert sentinel.read_text() == "operator sentinel\n"
    finally:
        for cleanup in reversed(cleanups):
            cleanup()
    assert os.environ.get("GOBBY_HOME") == initial_home
    if inherited_home != "isolated":
        assert not selected.exists()


def test_inherited_managed_grant_is_cleared_before_database_operations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gobby.agents.constants import ALL_TERMINAL_ENV_VARS
    from gobby.storage.hub.managed import managed_grant_path
    from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV
    from gobby.utils.local_token import GOBBY_MANAGED_EXECUTION_ID_ENV

    identity_names = {
        name
        for name in ALL_TERMINAL_ENV_VARS
        if name.startswith("GOBBY_") and name != "GOBBY_DAEMON_URL"
    } | {MANAGED_EXECUTION_BOOTSTRAP_ENV, GOBBY_MANAGED_EXECUTION_ID_ENV}
    for name in identity_names:
        monkeypatch.setenv(name, str(tmp_path / "inherited-identity"))
    clear_identity = cast(
        Callable[[pytest.MonkeyPatch], None], unwrap(_clear_invoking_agent_identity)
    )
    clear_identity(monkeypatch)

    assert managed_grant_path() is None
    assert identity_names.isdisjoint(os.environ)


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
