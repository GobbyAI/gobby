"""Managed envelopes cannot be re-admitted through foreign-run grants."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_policy import (
    SRT_SETTINGS_RELATIVE_PATH,
    assert_sensitive_path_contract,
    managed_execution_root,
    sensitive_roots,
)
from gobby.agents.srt_runtime import SrtRuntimeError, prepare_sandbox_launch

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_kind", ["computed-paths", "launch"])
@pytest.mark.parametrize("grant_kind", ["read", "write"])
@pytest.mark.parametrize(
    "path_kind", ["literal", "symlink", "glob", "glob-ancestor", "glob-own", "glob-foreign"]
)
async def test_foreign_envelope_grants_fail_before_policy_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_kind: str,
    grant_kind: str,
    path_kind: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    managed = managed_execution_root()
    own = managed / "own" / "grant.json"
    foreign = managed / "foreign" / "grant.json"
    for path in (own, foreign):
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    alias = tmp_path / "foreign-alias"
    alias.symlink_to(foreign.parent, target_is_directory=True)
    grants = {
        "literal": str(foreign),
        "symlink": str(alias / "grant.json"),
        "glob": str(managed / "*" / "grant.json"),
        "glob-ancestor": str(managed.parent / "*" / "*" / "grant.json"),
        "glob-own": str(own.parent / "assets" / "*.json"),
        "glob-foreign": str(foreign.parent / "assets" / "*.json"),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = workspace / "claude"
    provider.write_text("#!/bin/sh\n", encoding="utf-8")
    provider.chmod(0o755)
    verify = Mock(side_effect=AssertionError("foreign grants reached policy rendering"))
    monkeypatch.setattr("gobby.agents.srt_runtime.verify_srt_installation", verify)
    if guard_kind == "launch":
        # Exercise the launch guard independently of the earlier path contract.
        monkeypatch.setattr("gobby.agents.sandbox_policy.assert_sensitive_path_contract", Mock())
    config = SandboxConfig(
        enabled=True,
        backend="srt",
        allow_network=False,
        extra_read_paths=[grants[path_kind]] if grant_kind == "read" else [],
        extra_write_paths=[grants[path_kind]] if grant_kind == "write" else [],
    )

    expected_error: type[Exception]
    if guard_kind == "computed-paths" and path_kind.startswith("glob"):
        expected_error = ValueError
        expected_message = "contains sensitive root"
    else:
        expected_error = SrtRuntimeError
        expected_message = "another managed execution"
    with pytest.raises(expected_error, match=expected_message):
        await prepare_sandbox_launch(
            config=config,
            provider="claude",
            workspace_path=str(workspace),
            run_id="own",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={
                "PATH": str(workspace) + os.pathsep + os.environ.get("PATH", ""),
                "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(own),
            },
            prewarm_pre_commit_store=False,
        )
    verify.assert_not_called()
    assert not (own.parent / SRT_SETTINGS_RELATIVE_PATH).exists()


@pytest.mark.parametrize("glob", ["*", "?", "[ab]", "{own,foreign}"])
@pytest.mark.parametrize("root_kind", ["ancestor", "managed", "own"])
def test_sensitive_contract_rejects_managed_glob_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, glob: str, root_kind: str
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    managed = managed_execution_root()
    roots = {"ancestor": managed.parent, "managed": managed, "own": managed / "own"}
    grant = str(roots[root_kind] / glob / "grant.json")

    with pytest.raises(ValueError, match="contains sensitive root"):
        assert_sensitive_path_contract([grant])


def test_sensitive_contract_preserves_literal_own_and_unrelated_glob_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    own = managed_execution_root() / "own"
    assert_sensitive_path_contract(
        [str(own / "assets"), str(own / "cache")],
        [str(tmp_path / "workspace" / "*.json")],
    )


def test_sensitive_contract_detects_glob_before_symlink_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    own = managed_execution_root() / "own"
    own.mkdir(parents=True)
    glob = own / "*"
    glob.symlink_to(own, target_is_directory=True)

    with pytest.raises(ValueError, match="contains sensitive root"):
        assert_sensitive_path_contract([str(glob)])


def test_managed_root_denies_literal_and_resolved_home_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    alias = tmp_path / "home-alias"
    alias.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("GOBBY_HOME", str(alias))

    denies = sensitive_roots()

    assert str(alias / "runtime" / "managed-executions") in denies
    assert str(real_home / "runtime" / "managed-executions") in denies
