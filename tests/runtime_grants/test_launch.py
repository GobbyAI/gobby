"""Managed launch file and isolated child environment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gobby.runtime_grants import GrantBundle
from gobby.runtime_grants.launch import merge_child_env, write_grant_file

pytestmark = pytest.mark.unit

_GOLDEN = Path(__file__).resolve().parent / "golden" / "direct_datastores.json"


def _grant() -> GrantBundle:
    return GrantBundle.model_validate_json(_GOLDEN.read_bytes())


def test_merge_child_env_returns_none_for_missing_extra() -> None:
    assert merge_child_env(None) is None


def test_merge_child_env_omits_unsanctioned_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_LEAK", "should-not-copy")
    monkeypatch.setenv("PATH", "/bin")
    extra = {
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/tmp/grant.json",
        "GOBBY_AGENT_API_TOKEN": "tok",
        "OTHER": "nope",
    }

    env = merge_child_env(extra)

    assert env is not None
    assert env["PATH"] == "/bin"
    assert env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == "/tmp/grant.json"
    assert env["GOBBY_AGENT_API_TOKEN"] == "tok"
    assert "SECRET_LEAK" not in env
    assert "OTHER" not in env


def test_write_grant_file_closes_descriptor_if_chmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    real_close = os.close

    def boom(_descriptor: int, _mode: int) -> None:
        raise OSError("chmod failed")

    def spy_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "fchmod", boom)
    monkeypatch.setattr(os, "close", spy_close)

    with pytest.raises(OSError, match="chmod failed"):
        write_grant_file(tmp_path / "grant.json", _grant())

    assert closed
    assert not (tmp_path / "grant.json").exists()
