"""The handoff E2E's fixed project folder, and its promise never to touch provider trust.

Security review 49c502c1: an earlier proof seeded workspace trust in the operator's real
provider config. The seat uses the operator HOME for login, so the proof must neither write
nor depend on provider trust. It runs in a fixed plain folder under ~/.gobby (#25771).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_E2E = Path(__file__).parent / "e2e"
_PROOF_MODULES = (
    "handoff_continuation_fixture.py",
    "test_handoff_continuation_e2e.py",
    "handoff_continuation_project.py",
)
_TRUST_WRITERS = frozenset(
    {"pre_approve_directory", "seed_cli_trust", "seed_gobby_home_trust", "update_claude_config"}
)


def _called_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module", _PROOF_MODULES)
def test_the_proof_never_writes_provider_trust(module: str) -> None:
    path = _E2E / module
    assert path.exists(), f"{module} missing"
    assert not (_called_names(path) & _TRUST_WRITERS), f"{module} writes provider trust"


def test_the_project_folder_is_fixed_under_the_gobby_home() -> None:
    from tests.e2e.handoff_continuation_project import project_folder

    home = Path("/home/op")
    assert project_folder(home) == home / ".gobby" / "e2e" / "handoff-continuation-project"


def test_the_proof_folder_is_built_without_git(tmp_path: Path) -> None:
    from tests.e2e.conftest import build_e2e_project

    folder = tmp_path / "project"
    folder.mkdir()
    build_e2e_project(folder, git=False)

    assert (folder / ".gobby" / "project.json").is_file()
    assert not (folder / ".git").exists()


def test_only_the_e2e_owned_subtree_is_exempt_from_the_real_home_audit() -> None:
    from tests.e2e.handoff_continuation_project import project_folder
    from tests.fixtures.external_write_audit import is_e2e_owned

    home = Path("/home/op")
    folder = project_folder(home).relative_to(home / ".gobby").as_posix()
    assert is_e2e_owned(folder)
    assert is_e2e_owned(f"{folder}/.gobby/project.json")
    assert not is_e2e_owned("e2e-auth/credentials")
    assert not is_e2e_owned("bootstrap.yaml")
    assert not is_e2e_owned("logs/e2e/x")


def test_the_proof_folder_is_removed_with_its_read_only_runtime_tree(tmp_path: Path) -> None:
    # The isolated daemon's HOME lives inside the folder and installs its sandbox runtime
    # read-only; a silent partial removal left the folder behind and broke the next case.
    from tests.e2e.handoff_continuation_project import remove_project_folder

    folder = tmp_path / "project"
    runtime = folder / ".gobby-home" / "tools" / "srt" / "0.0.79"
    runtime.mkdir(parents=True)
    (runtime / "cli.js").write_text("x")
    (runtime / "cli.js").chmod(0o444)
    runtime.chmod(0o555)
    runtime.parent.chmod(0o555)

    remove_project_folder(folder)

    assert not folder.exists()
    remove_project_folder(folder)
