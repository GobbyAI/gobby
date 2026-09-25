"""Fixture probes for the default profile's documented session-role lookup."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ID = json.loads((REPO_ROOT / ".gobby/project.json").read_text())["id"]


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


@pytest.fixture
def older_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    old = tmp_path / "old"
    _git("init", "-q", "-b", "main", str(main))
    _git("-C", str(main), "config", "user.email", "roles@example.invalid")
    _git("-C", str(main), "config", "user.name", "Role Test")
    (main / ".gobby").mkdir()
    (main / ".gobby/project.json").write_text(json.dumps({"id": PROJECT_ID}))
    _git("-C", str(main), "add", ".gobby/project.json")
    _git("-C", str(main), "commit", "-qm", "base")
    _git("-C", str(main), "worktree", "add", "-qb", "old", str(old))
    shutil.copytree(REPO_ROOT / ".gobby/roles", main / ".gobby/roles")
    assert not (old / ".gobby/roles").exists()
    return old, main


def _probe_documented_lookup(
    checkout: Path, session_ref: str, *, env: dict[str, str] | None = None
) -> tuple[Path, Path] | None:
    """Exercise the filesystem/Git preconditions stated by default.yaml."""
    if re.fullmatch(r"gobby#\d+", session_ref) is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode:
        return None
    common_dir = Path(result.stdout.strip())
    if common_dir.name != ".git" or not common_dir.is_dir():
        return None
    shared = common_dir.parent
    try:
        if any(
            json.loads((root / ".gobby/project.json").read_text())["id"] != PROJECT_ID
            for root in (checkout, shared)
        ):
            return None
        roles = shared / ".gobby/roles"
        common_file = roles / "_common.md"
        common_file.read_text()
        roster = (roles / "roster.md").read_text()
        matches = re.findall(rf"^\| ([a-z0-9-]+\.md) \| {re.escape(session_ref)} \|$", roster, re.M)
        if len(matches) != 1:
            return None
        role_file = roles / matches[0]
        role_file.read_text()
    except (OSError, KeyError, ValueError):
        return None
    return common_file, role_file


def test_default_profile_describes_all_lookup_guards() -> None:
    path = get_bundled_agents_path() / "default.yaml"
    body = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))
    for prompt in (body.prompts.persona, body.prompts.agent):
        assert prompt is not None
        for required in (
            "Gobby Session ID",
            "--git-common-dir",
            PROJECT_ID,
            "Read `_common.md` first, then `roster.md`",
            "Session cell exactly matches your ref",
            "continue with this generic profile",
        ):
            assert required in prompt


def test_old_worktree_uses_shared_checkout_and_exact_role(
    older_worktree: tuple[Path, Path],
) -> None:
    old, main = older_worktree
    assert _probe_documented_lookup(old, "gobby#14549") == (
        main / ".gobby/roles/_common.md",
        main / ".gobby/roles/rust-migration.md",
    )
    assert _probe_documented_lookup(old, "gobby#99999") is None
    assert _probe_documented_lookup(old, "other#14549") is None


@pytest.mark.parametrize(
    "defect",
    [
        "missing_git",
        "invalid_common_dir",
        "different_project",
        "missing_common",
        "missing_row",
        "missing_role",
    ],
)
def test_invalid_role_lookup_preconditions_fall_back(
    older_worktree: tuple[Path, Path], tmp_path: Path, defect: str
) -> None:
    old, main = older_worktree
    env = None
    if defect == "missing_git":
        old = tmp_path
    elif defect == "invalid_common_dir":
        env = {**os.environ, "GIT_COMMON_DIR": str(tmp_path / "missing-git")}
    elif defect == "different_project":
        (old / ".gobby/project.json").write_text('{"id":"unrelated-project"}')
    elif defect == "missing_common":
        (main / ".gobby/roles/_common.md").unlink()
    elif defect == "missing_row":
        roster = main / ".gobby/roles/roster.md"
        roster.write_text(roster.read_text().replace("| rust-migration.md | gobby#14549 |\n", ""))
    elif defect == "missing_role":
        (main / ".gobby/roles/rust-migration.md").unlink()
    assert _probe_documented_lookup(old, "gobby#14549", env=env) is None
