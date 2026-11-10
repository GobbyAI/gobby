"""The shared SRT policy hides the operator's home except the runtime root and interpreter bases.

SRT 0.0.79 reads are deny-then-allow: every read is allowed by default, `denyRead` hides a
region, and an `allowRead` entry re-opens a path inside it and wins over `denyRead`. `readable`
evaluates a policy under exactly that rule. Every file here is synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import c0
import pytest
from launch_daemon import (
    MONITOR_SOCKET_ENV,
    OPERATOR_HOME_DENY_READS,
    resolve_srt_runner,
    srt_policy,
    srt_wrapped_argv,
)
from record_installations import third_party_probe_policy
from runtime_boundary import EvidenceError


def _under(path: Path, entries: list[str]) -> bool:
    return any(path.is_relative_to(entry) for entry in entries)


def readable(policy: dict[str, Any], path: Path) -> bool:
    filesystem = policy["filesystem"]
    if _under(path, filesystem["allowRead"]):
        return True
    return not _under(path, filesystem["denyRead"])


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    root = home / ".gobby/agents_workspaces/project/epics/epic-1/build/rerun"
    root.mkdir(parents=True)
    return home, root


def test_outside_root_files_under_home_are_denied_and_the_root_stays_readable(
    tmp_path: Path,
) -> None:
    home, root = _layout(tmp_path)
    synthetic = home / ".ssh/id_synthetic"
    synthetic.parent.mkdir()
    synthetic.write_text("synthetic\n")
    for policy in (
        srt_policy(root, home),
        c0.c0_policy(root, home, root / c0.GRAPHIFY_INTERPRETER),
        third_party_probe_policy(root, home, root / "tmp/probe"),
    ):
        assert not readable(policy, synthetic)
        assert not readable(policy, home / ".aws/credentials")
        assert readable(policy, root / "receipts/any.json")
        for secret in OPERATOR_HOME_DENY_READS:
            assert not readable(policy, home / secret)


def test_interpreter_bases_outside_the_root_are_re_allowed(tmp_path: Path) -> None:
    home, root = _layout(tmp_path)
    base = home / ".local/share/uv/python/cpython-3.13"
    (base / "bin").mkdir(parents=True)
    (base / "bin/python3.13").touch()
    for venv in ("gobby", "graphify"):
        interpreter = root / f"tools/{venv}/venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(base / "bin/python3.13")
    policy = srt_policy(root, home)
    assert policy["filesystem"]["allowRead"] == [str(root), str(base.resolve())]
    assert readable(policy, base.resolve() / "lib/python3.13/os.py")
    assert not readable(policy, home / ".local/share/other/secret")


@pytest.mark.parametrize("relative", ["", ".gobby", ".codex"])
def test_a_carve_out_that_would_expose_home_or_a_named_secret_is_refused(
    tmp_path: Path, relative: str
) -> None:
    home = tmp_path / "home"
    with pytest.raises(EvidenceError, match="would re-allow"):
        srt_policy(home / relative if relative else home, home)


@pytest.mark.skipif(
    os.environ.get("GOBBY_HARNESS_LIVE_SRT") != "1",
    reason="starts the pinned SRT and its shared monitor; set GOBBY_HARNESS_LIVE_SRT=1",
)
def test_pinned_srt_denies_a_synthetic_home_file_and_reads_the_root(tmp_path: Path) -> None:
    home, root = _layout(tmp_path)
    synthetic = home / ".ssh/id_synthetic"
    synthetic.parent.mkdir()
    synthetic.write_text("synthetic-secret\n")
    (root / "ok.txt").write_text("runtime-ok\n")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(srt_policy(root, home)))
    violations = tmp_path / "violations.jsonl"
    violations.touch(mode=0o600)
    srt = resolve_srt_runner(root, "live-srt-test")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
    if srt.monitor_socket is not None:
        env[MONITOR_SOCKET_ENV] = srt.monitor_socket
        env["GOBBY_SRT_RUN_KEY"] = "live-srt-test"

    def cat(path: Path) -> subprocess.CompletedProcess[str]:
        argv = srt_wrapped_argv(
            ["/bin/cat", str(path)], policy=policy_path, violations=violations, srt=srt
        )
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)

    allowed = cat(root / "ok.txt")
    assert allowed.returncode == 0
    assert allowed.stdout == "runtime-ok\n"
    denied = cat(synthetic)
    assert denied.returncode != 0
    assert "synthetic-secret" not in denied.stdout
