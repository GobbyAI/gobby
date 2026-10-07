"""Owner-only recovery credential file contracts."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("damaged", [b"", b"partial"])
def test_damaged_existing_credential_requires_explicit_repair(
    tmp_path: Path, damaged: bytes
) -> None:
    from gobby.utils.break_glass import ensure_break_glass_credential

    path = tmp_path / "break_glass"
    path.write_bytes(damaged)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="remove.*restart") as error:
        ensure_break_glass_credential(tmp_path)
    assert str(path) in str(error.value)
    assert path.read_bytes() == damaged


def test_failed_fsync_never_publishes_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.break_glass import ensure_break_glass_credential

    def failed_fsync(_fd: int) -> None:
        raise OSError("simulated full disk")

    monkeypatch.setattr(os, "fsync", failed_fsync)
    with pytest.raises(OSError, match="simulated full disk"):
        ensure_break_glass_credential(tmp_path)
    assert not (tmp_path / "break_glass").exists()
    assert list((tmp_path / ".break_glass-staging").iterdir()) == []


def test_exclusive_publication_preserves_a_concurrent_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.break_glass import ensure_break_glass_credential

    winner = b"W" * 43

    def winning_link(_source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        path = Path(destination)
        path.write_bytes(winner)
        path.chmod(0o600)
        raise FileExistsError(path)

    monkeypatch.setattr(os, "link", winning_link)
    ensure_break_glass_credential(tmp_path)
    assert (tmp_path / "break_glass").read_bytes() == winner
    assert list((tmp_path / ".break_glass-staging").iterdir()) == []


def test_staging_symlink_cannot_redirect_credential_bytes(
    tmp_path: Path,
) -> None:
    from gobby.utils.break_glass import ensure_break_glass_credential

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".break_glass-staging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        ensure_break_glass_credential(tmp_path)
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "break_glass").exists()


def test_credential_is_owner_only_and_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.break_glass import (
        break_glass_matches,
        break_glass_path,
        ensure_break_glass_credential,
    )

    ensure_break_glass_credential(tmp_path)
    path = break_glass_path(tmp_path)
    original = path.read_bytes()
    assert len(original) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
    ensure_break_glass_credential(tmp_path)
    assert path.read_bytes() == original
    value = original.decode().strip()
    assert break_glass_matches(path, value)
    assert not break_glass_matches(path, "wrong")
    assert not break_glass_matches(path, "wrong\u2603")
    for mode in (0o640, 0o601):
        path.chmod(mode)
        assert not break_glass_matches(path, value)
    path.chmod(0o600)
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    assert not break_glass_matches(path, value)


def test_missing_empty_unreadable_and_symlink_credentials_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.break_glass import break_glass_matches

    path = tmp_path / "break_glass"
    assert not break_glass_matches(path, "value")
    path.touch(mode=0o600)
    assert not break_glass_matches(path, "")
    path.write_text("value")
    link = tmp_path / "link"
    link.symlink_to(path)
    assert not break_glass_matches(link, "value")

    def denied_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("unreadable test credential")

    with monkeypatch.context() as context:
        context.setattr(os, "open", denied_open)
        assert not break_glass_matches(path, "value")


def test_bootstrap_binding_controls_only_daemon_credential_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils import local_token
    from gobby.utils.break_glass import break_glass_path

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(local_token, "_daemon_bootstrap", None)
    assert local_token.daemon_bootstrap_path() == tmp_path / "home/bootstrap.yaml"
    bound = tmp_path / "outside/bootstrap.yaml"
    local_token.bind_daemon_bootstrap(bound)
    assert local_token.daemon_bootstrap_path() == bound
    assert break_glass_path() == bound.parent / "break_glass"
    assert local_token.local_token_path() == tmp_path / "home/local_cli_token"
