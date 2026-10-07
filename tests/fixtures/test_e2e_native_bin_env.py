"""Native binary dir selection for e2e daemons, kept outside the e2e autouse-fixture subtree."""

import errno
import os
from pathlib import Path

import pytest

from gobby.utils.native_bin import IDENTITY_STAMP_NAME, NATIVE_BIN_DIR_ENV, native_bin_name
from tests.e2e import conftest as e2e_fixtures

pytestmark = pytest.mark.unit

GDAEMON = native_bin_name("gdaemon")
GTERM = native_bin_name("gterm")


@pytest.mark.parametrize("pinned", [False, True], ids=["default-bin", "explicit-pin"])
def test_installed_daemon_bin_and_locks_are_isolated(
    installed_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("gobby.utils.native_bin.native_bin_dir", lambda: installed_dir)
    base = {"GOBBY_TEST_GDAEMON": "installed"}
    if pinned:
        base[NATIVE_BIN_DIR_ENV] = str(installed_dir)
    env = e2e_fixtures.prepare_daemon_env(base, home_dir=home)
    isolated = Path(env[NATIVE_BIN_DIR_ENV])
    assert isolated.is_relative_to(home)
    assert (isolated / IDENTITY_STAMP_NAME).read_bytes() == (
        installed_dir / IDENTITY_STAMP_NAME
    ).read_bytes()
    for binary in (GDAEMON, GTERM):
        assert (isolated / binary).read_bytes() == (installed_dir / binary).read_bytes()
        assert not os.path.samefile(isolated / binary, installed_dir / binary)
    monkeypatch.setenv(NATIVE_BIN_DIR_ENV, str(isolated))
    lock = try_acquire_native_bin_lock("gdaemon", bin_dir=isolated)
    assert lock is not None
    lock.release()
    assert (isolated / ".locks" / "gdaemon.lock").is_file()
    assert not (installed_dir / ".locks").exists()
    original = (installed_dir / GTERM).read_bytes()
    (isolated / GTERM).write_bytes(b"isolated update")
    assert (installed_dir / GTERM).read_bytes() == original


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n# {path}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def checkout_gdaemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installed_dir: Path) -> Path:
    binary = _executable(tmp_path / "checkout" / "target" / "debug" / GDAEMON)

    def select(_root: Path, env: dict[str, str], _name: str) -> Path | None:
        return binary if env.get("GOBBY_TEST_GDAEMON") == "checkout" else None

    monkeypatch.setattr("tests.fixtures.gdaemon_binary.select_test_gdaemon", select)
    monkeypatch.setattr("gobby.utils.native_bin.native_bin_dir", lambda: installed_dir)
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
        # gterm pins its own executable, so a symlinked gterm can never host.
        assert not (bin_dir / GTERM).is_symlink()
        assert (bin_dir / GTERM).read_bytes() == (installed_dir / GTERM).read_bytes()
        assert not os.path.samefile(bin_dir / GTERM, installed_dir / GTERM)
        assert not (bin_dir / IDENTITY_STAMP_NAME).exists()
    assert first != second
    assert sorted(entry.name for entry in installed_dir.iterdir()) == before
    assert dict(os.environ) == environ_before


def test_pinned_files_are_copied_where_links_are_refused(
    checkout_gdaemon: Path,
    installed_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandbox that cannot write the pinned dir refuses every link, so its files are copied."""
    runtime = installed_dir / ".ghook-runtime.json"
    runtime.write_text('{"runtime": "installed"}')
    runtime.chmod(0o644)
    backup = _executable(installed_dir / ".gcode.bak-v435")

    def sandboxed_link(src: Path, dst: Path) -> None:
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), str(src))

    monkeypatch.setattr(os, "link", sandboxed_link)
    home = tmp_path / "home"
    home.mkdir()
    base = {"GOBBY_TEST_GDAEMON": "checkout", NATIVE_BIN_DIR_ENV: str(installed_dir)}

    bin_dir = Path(e2e_fixtures.prepare_daemon_env(base, home_dir=home)[NATIVE_BIN_DIR_ENV])

    for pinned in (runtime, backup, installed_dir / GTERM):
        copied = bin_dir / pinned.name
        assert copied.read_bytes() == pinned.read_bytes()
        assert not copied.is_symlink()
        assert not os.path.samefile(copied, pinned)
        assert copied.stat().st_mode == pinned.stat().st_mode
    assert (bin_dir / GDAEMON).resolve() == checkout_gdaemon.resolve()
    assert not (bin_dir / IDENTITY_STAMP_NAME).exists()


def test_cross_filesystem_pin_is_copied_without_links(
    checkout_gdaemon: Path,
    installed_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned dir need not share a filesystem with the isolated home."""

    def cross_device(src: Path, dst: Path) -> None:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src), None, str(dst))

    monkeypatch.setattr(os, "link", cross_device)
    base = {"GOBBY_TEST_GDAEMON": "checkout", NATIVE_BIN_DIR_ENV: str(installed_dir)}

    isolated = Path(e2e_fixtures.prepare_daemon_env(base, home_dir=tmp_path)[NATIVE_BIN_DIR_ENV])
    assert (isolated / GTERM).read_bytes() == (installed_dir / GTERM).read_bytes()
    assert not os.path.samefile(isolated / GTERM, installed_dir / GTERM)


@pytest.mark.parametrize(
    ("selector", "pinned", "expected"),
    [
        ("checkout", False, "checkout"),
        ("checkout", True, "checkout"),
        ("installed", True, "installed"),
    ],
    ids=["checkout-unpinned", "checkout-pinned-to-checkout", "installed-pinned"],
)
def test_unconflicted_selection_is_isolated_under_test_home(
    checkout_gdaemon: Path,
    installed_dir: Path,
    tmp_path: Path,
    selector: str,
    pinned: bool,
    expected: str,
) -> None:
    """Even an unconflicted pin must isolate all native-set writes."""
    dirs = {"checkout": checkout_gdaemon.parent, "installed": installed_dir}
    base = {"GOBBY_TEST_GDAEMON": selector}
    if pinned:
        base[NATIVE_BIN_DIR_ENV] = str(dirs[expected])

    env = e2e_fixtures.prepare_daemon_env(base, home_dir=tmp_path)

    isolated = Path(env[NATIVE_BIN_DIR_ENV])
    assert isolated.is_relative_to(tmp_path)
    if selector == "checkout":
        assert (isolated / GDAEMON).resolve() == checkout_gdaemon.resolve()
        assert not (isolated / IDENTITY_STAMP_NAME).exists()
    else:
        assert (isolated / GDAEMON).read_bytes() == (installed_dir / GDAEMON).read_bytes()
        assert (isolated / IDENTITY_STAMP_NAME).read_bytes() == (
            installed_dir / IDENTITY_STAMP_NAME
        ).read_bytes()
