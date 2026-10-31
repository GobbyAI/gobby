"""Glob grants must preserve sensitive-path boundaries."""

from pathlib import Path

import pytest

from gobby.agents import sandbox_policy

pytestmark = pytest.mark.unit


def test_glob_grants_only_reach_matching_sensitive_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "managed-executions"
    monkeypatch.setattr(sandbox_policy, "sensitive_roots", lambda: [str(protected)])
    monkeypatch.setattr(sandbox_policy, "get_gobby_home", lambda: tmp_path / "gobby")
    monkeypatch.setattr(sandbox_policy, "private_srt_socket_roots", lambda: [])

    with pytest.raises(ValueError, match="sandbox allow path contains sensitive root"):
        sandbox_policy.assert_sensitive_path_contract([str(tmp_path)])
    with pytest.raises(ValueError, match="sandbox allow path contains sensitive root"):
        sandbox_policy.assert_sensitive_path_contract([str(tmp_path / "managed-*")])

    sandbox_policy.assert_sensitive_path_contract([str(tmp_path / "xcrun_db-*")])
