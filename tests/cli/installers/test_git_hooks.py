"""Focused acceptance tests for generated git-hook authentication."""

import os
import subprocess
from pathlib import Path

import pytest

from gobby.cli.installers.git_hooks import _CODE_INDEX_REINDEX_BODY

pytestmark = pytest.mark.unit


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_hook_body(
    work_dir: Path,
    token: str | None,
    *,
    jq_available: bool,
    agent_token: str | None = None,
    strict_unset: bool = False,
) -> list[str]:
    home = work_dir / "home"
    gobby_home = work_dir / "gobby-home"
    fake_bin = work_dir / "bin"
    capture = work_dir / "curl-args"

    _write_executable(home / ".gobby/bin/gcode", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "git", '#!/bin/sh\nprintf "%s\\n" "$HOOK_ROOT"\n')
    _write_executable(
        fake_bin / "curl",
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_CAPTURE"\n',
    )
    _write_executable(fake_bin / "grep", "#!/bin/sh\nexit 1\n")
    _write_executable(fake_bin / "sed", '#!/bin/sh\nIFS= read -r line\nprintf "%s\\n" "$line"\n')
    _write_executable(fake_bin / "tr", '#!/bin/sh\nIFS= read -r line\nprintf "%s\\n" "$line"\n')
    _write_executable(fake_bin / "xargs", '#!/bin/sh\nshift\n"$@"\n')
    if jq_available:
        _write_executable(
            fake_bin / "jq",
            '#!/bin/sh\nprintf \'{"root_path":"%s"}\\n\' "$HOOK_ROOT"\n',
        )

    if token is not None:
        gobby_home.mkdir(parents=True)
        (gobby_home / "local_cli_token").write_text(f"{token}\n", encoding="utf-8")

    env = os.environ | {
        "CHANGED_FILES": "changed.py",
        "CURL_CAPTURE": str(capture),
        "GOBBY_HOME": str(gobby_home),
        "HOME": str(home),
        "HOOK_ROOT": str(work_dir),
        "PATH": str(fake_bin),
    }
    if agent_token is not None:
        env.update(
            {
                "GOBBY_AGENT_API_TOKEN": agent_token,
                "GOBBY_AGENT_RUN_ID": "run-123",
                "GOBBY_PROJECT_ID": "project-123",
                "GOBBY_SESSION_ID": "session-123",
            }
        )
    prelude = "set -u\n" if strict_unset else ""
    result = subprocess.run(
        ["/bin/bash", "-c", f"{prelude}{_CODE_INDEX_REINDEX_BODY}\nwait"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    return capture.read_text(encoding="utf-8").splitlines()


def test_hook_body_includes_token(tmp_path: Path) -> None:
    for branch, jq_available in (("jq", True), ("fallback", False)):
        token_args = _run_hook_body(
            tmp_path / branch / "with-token",
            "test-token",
            jq_available=jq_available,
        )
        anonymous_args = _run_hook_body(
            tmp_path / branch / "without-token",
            None,
            jq_available=jq_available,
        )

        assert "Authorization: Bearer test-token" in token_args
        assert token_args.count("-H") == 2
        assert anonymous_args.count("-H") == 1
        assert all(not arg.startswith("Authorization:") for arg in anonymous_args)


def test_hook_body_prefers_scoped_agent_token_and_identity(tmp_path: Path) -> None:
    args = _run_hook_body(
        tmp_path,
        "operator-token",
        jq_available=True,
        agent_token="scoped-agent-token",
    )

    assert "Authorization: Bearer scoped-agent-token" in args
    assert "Authorization: Bearer operator-token" not in args
    assert "X-Gobby-Agent-Run-Id: run-123" in args
    assert "X-Gobby-Project-Id: project-123" in args
    assert "X-Gobby-Session-Id: session-123" in args


def test_hook_body_survives_set_u_without_agent_identity(tmp_path: Path) -> None:
    """Chained user hooks run under set -u; unset GOBBY_* vars and empty
    header arrays (bash 3.2 treats them as unbound) must not abort."""
    args = _run_hook_body(
        tmp_path / "with-token",
        "operator-token",
        jq_available=True,
        strict_unset=True,
    )
    anonymous_args = _run_hook_body(
        tmp_path / "anonymous",
        None,
        jq_available=True,
        strict_unset=True,
    )

    assert "Authorization: Bearer operator-token" in args
    assert all(not arg.startswith("X-Gobby-") for arg in args)
    assert all(not arg.startswith("Authorization:") for arg in anonymous_args)
