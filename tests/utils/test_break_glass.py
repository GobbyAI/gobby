"""Owner-only recovery credential file contracts."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


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
