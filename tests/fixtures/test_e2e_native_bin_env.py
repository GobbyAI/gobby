"""Native binary dir selection for e2e daemons, kept outside the e2e autouse-fixture subtree."""

import os
from pathlib import Path

import pytest

from gobby.utils.native_bin import IDENTITY_STAMP_NAME, NATIVE_BIN_DIR_ENV, native_bin_name
from tests.e2e import conftest as e2e_fixtures

pytestmark = pytest.mark.unit

GDAEMON = native_bin_name("gdaemon")
GTERM = native_bin_name("gterm")


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n# {path}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def checkout_gdaemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    binary = _executable(tmp_path / "checkout" / "target" / "debug" / GDAEMON)

    def select(_root: Path, env: dict[str, str], _name: str) -> Path | None:
        return binary if env.get("GOBBY_TEST_GDAEMON") == "checkout" else None

    monkeypatch.setattr("tests.fixtures.gdaemon_binary.select_test_gdaemon", select)
    return binary


@pytest.fixture
def installed_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "installed"
    _executable(bin_dir / GDAEMON)
    _executable(bin_dir / GTERM)
    (bin_dir / IDENTITY_STAMP_NAME).write_text('{"schema": "installed"}')
    return bin_dir


def test_checkout_gdaemon_survives_pinned_gterm_dir(
    checkout_gdaemon: Path, installed_dir: Path, tmp_path: Path
) -> None:
    """A test pinning the gterm dir still gets the checkout gdaemon beside that gterm."""
    before = sorted(entry.name for entry in installed_dir.iterdir())
    environ_before = dict(os.environ)
    home = tmp_path / "home"
    home.mkdir()
    base = {"GOBBY_TEST_GDAEMON": "checkout", NATIVE_BIN_DIR_ENV: str(installed_dir)}

    first = Path(e2e_fixtures.prepare_daemon_env(base, home_dir=home)[NATIVE_BIN_DIR_ENV])
    second = Path(e2e_fixtures.prepare_daemon_env(base, home_dir=home)[NATIVE_BIN_DIR_ENV])

    for bin_dir in (first, second):
        assert (bin_dir / GDAEMON).resolve() == checkout_gdaemon.resolve()
        assert (bin_dir / GTERM).resolve() == (installed_dir / GTERM).resolve()
        assert not (bin_dir / IDENTITY_STAMP_NAME).exists()
    assert first != second
    assert sorted(entry.name for entry in installed_dir.iterdir()) == before
    assert dict(os.environ) == environ_before


@pytest.mark.parametrize(
    ("selector", "pinned", "expected"),
    [
        ("checkout", False, "checkout"),
        ("checkout", True, "checkout"),
        ("installed", True, "installed"),
    ],
    ids=["checkout-unpinned", "checkout-pinned-to-checkout", "installed-pinned"],
)
def test_unconflicted_selection_keeps_its_bin_dir(
    checkout_gdaemon: Path,
    installed_dir: Path,
    tmp_path: Path,
    selector: str,
    pinned: bool,
    expected: str,
) -> None:
    """Only a pin that would hide the checkout gdaemon gets a composite dir."""
    dirs = {"checkout": checkout_gdaemon.parent, "installed": installed_dir}
    base = {"GOBBY_TEST_GDAEMON": selector}
    if pinned:
        base[NATIVE_BIN_DIR_ENV] = str(dirs[expected])

    env = e2e_fixtures.prepare_daemon_env(base, home_dir=tmp_path)

    assert env[NATIVE_BIN_DIR_ENV] == str(dirs[expected])
