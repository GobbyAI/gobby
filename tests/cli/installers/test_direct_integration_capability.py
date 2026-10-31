"""The installed Git guard obeys the committing session's rule verdict."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from gobby.tasks import landing_policy
from tests.cli.installers.test_landing_guard_hook import _Repo

pytestmark = pytest.mark.unit


def _integration_repo(tmp_path: Path) -> _Repo:
    repo = _Repo(tmp_path)
    # Bypass macOS's /usr/bin/git launcher, whose shared xcrun cache is outside
    # the isolated home. Other platforms can keep the fixture's normal Git.
    exec_path = Path(subprocess.check_output(["git", "--exec-path"], text=True).strip())
    native_git = exec_path.parent.parent / "bin" / "git"
    if native_git.is_file():
        git_link = tmp_path / "bin" / "git"
        git_link.unlink()
        git_link.symlink_to(native_git)
    repo.ok(repo.root, "symbolic-ref", "HEAD", "refs/heads/0.5.0")
    initial = repo.commit(repo.root, {"README.md": "base\n"})
    assert initial.returncode == 0, initial.stderr
    return repo


@pytest.mark.parametrize(
    ("path", "decision", "exit_code", "allowed"),
    [
        ("src/gobby/install/shared/workflows/agents/custom-seat.yaml", "allow", 0, True),
        ("docs/audit.md", "block", 2, False),
        (".gobby/plans/coverage/audit/manifest.yaml", "block", 2, False),
    ],
)
def test_reference_transaction_obeys_ghook_verdict(
    tmp_path: Path, path: str, decision: str, exit_code: int, allowed: bool
) -> None:
    repo = _integration_repo(tmp_path)
    log = tmp_path / "ghook-calls"
    fake = tmp_path / "bin" / "ghook"
    verdict = json.dumps({"decision": decision, "reason": "Use gobby-tasks-ops:land_commit"})
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        f"cat >> {shlex.quote(str(log))}\n"
        f"printf '%s\\n' {shlex.quote(verdict)}\n"
        f"exit {exit_code}\n"
    )
    fake.chmod(0o755)
    installed_bin = tmp_path / "home" / ".gobby" / "bin"
    installed_bin.mkdir(parents=True)
    (installed_bin / "ghook").symlink_to(fake)
    repo.env["GOBBY_SESSION_ID"] = "11111111-1111-4111-8111-111111111111"

    result = repo.commit(repo.root, {path: "changed\n"})

    assert (result.returncode == 0) is allowed, result.stderr
    assert log.is_file(), "Every direct integration update must consult ghook."
    if not allowed:
        assert "land_commit" in result.stderr


def test_missing_verdict_cannot_keep_the_old_markdown_bypass(tmp_path: Path) -> None:
    repo = _integration_repo(tmp_path)

    result = repo.commit(repo.root, {"docs/audit.md": "changed\n"})

    assert result.returncode != 0, "No ghook verdict means no direct integration grant."
    assert "land_commit" in result.stderr


def test_direct_commit_path_constants_are_removed() -> None:
    assert not hasattr(landing_policy, "DIRECT_COMMIT_MARKDOWN_DIRS")
    assert not hasattr(landing_policy, "DIRECT_COMMIT_ANY_FILE_DIRS")
