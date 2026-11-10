"""The handoff E2E only reads provider trust; it never writes the operator's config (#25771).

Security review 49c502c1: the proof seeded workspace trust in the operator's real provider
config. The seat must use the operator HOME for login, so the proof runs in a fixed folder
under ~/.gobby that the operator already trusts, and refuses fast when it is not trusted.
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
    "handoff_continuation_trust.py",
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
    from tests.e2e.handoff_continuation_trust import project_folder

    home = Path("/home/op")
    assert project_folder(home) == home / ".gobby" / "e2e" / "handoff-continuation-project"


def test_claude_trust_is_read_from_the_folder_or_an_ancestor() -> None:
    from tests.e2e.handoff_continuation_trust import claude_trusts

    folder = Path("/home/op/.gobby/e2e/handoff-continuation-project")
    gobby_trusted = {"projects": {"/home/op/.gobby": {"hasTrustDialogAccepted": True}}}
    assert claude_trusts(gobby_trusted, folder)
    # An entry the CLI records for the folder itself does not revoke an ancestor's trust.
    recorded = {
        "projects": {
            "/home/op/.gobby": {"hasTrustDialogAccepted": True},
            str(folder): {"hasTrustDialogAccepted": False},
        }
    }
    assert claude_trusts(recorded, folder)
    assert not claude_trusts({"projects": {"/home/op": {"hasTrustDialogAccepted": False}}}, folder)
    assert not claude_trusts({}, folder)
    assert not claude_trusts({"projects": []}, folder)


def test_codex_trust_is_read_from_the_folder_or_an_ancestor() -> None:
    from tests.e2e.handoff_continuation_trust import codex_trusts

    folder = Path("/home/op/.gobby/e2e/handoff-continuation-project")
    assert codex_trusts({"projects": {"/home/op/.gobby": {"trust_level": "trusted"}}}, folder)
    assert codex_trusts({"projects": {str(folder): {"trust_level": "trusted"}}}, folder)
    assert not codex_trusts({"projects": {"/home/op/.gobby": {"trust_level": "untrusted"}}}, folder)
    assert not codex_trusts({}, folder)


def test_an_untrusted_folder_is_refused_with_the_operator_step(tmp_path: Path) -> None:
    from tests.e2e.composer_proof import ProofRefused
    from tests.e2e.handoff_continuation_trust import require_provider_trust

    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"projects": {}}')
    folder = home / ".gobby" / "e2e" / "handoff-continuation-project"

    with pytest.raises(ProofRefused) as refused:
        require_provider_trust("claude", folder, home=home)

    message = str(refused.value)
    assert str(folder) in message
    assert "trust" in message


def test_a_trusted_folder_passes_and_the_config_is_untouched(tmp_path: Path) -> None:
    from tests.e2e.handoff_continuation_trust import require_provider_trust

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config = home / ".codex" / "config.toml"
    text = f'[projects."{home / ".gobby"}"]\ntrust_level = "trusted"\n'
    config.write_text(text)
    folder = home / ".gobby" / "e2e" / "handoff-continuation-project"

    require_provider_trust("codex", folder, home=home)

    assert config.read_text() == text


def test_only_the_e2e_owned_subtree_is_exempt_from_the_real_home_audit() -> None:
    from tests.e2e.handoff_continuation_trust import project_folder
    from tests.fixtures.external_write_audit import is_e2e_owned

    home = Path("/home/op")
    folder = project_folder(home).relative_to(home / ".gobby").as_posix()
    assert is_e2e_owned(folder)
    assert is_e2e_owned(f"{folder}/.gobby/project.json")
    assert not is_e2e_owned("e2e-auth/credentials")
    assert not is_e2e_owned("bootstrap.yaml")
    assert not is_e2e_owned("logs/e2e/x")


def test_a_provider_without_a_read_only_trust_check_is_refused(tmp_path: Path) -> None:
    from tests.e2e.composer_proof import ProofRefused
    from tests.e2e.handoff_continuation_trust import require_provider_trust

    with pytest.raises(ProofRefused):
        require_provider_trust("grok", tmp_path / "folder", home=tmp_path)
