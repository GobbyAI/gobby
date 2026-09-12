"""Regression boundaries for #22213, including merged #22233 generated output."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.code_navigation_recovery import navigation_requires_index
from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("web/dist/\ncustom-output/\n")
    (root / "custom-output").mkdir()
    (root / "src").mkdir()
    (root / "src/constants.py").write_text("VALUE = 1\n")
    return root


def event(repo: Path, command: str, output: str = "", *, failed: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo),
        "project_path": str(repo),
        "tool_output": output,
        "is_error": failed,
    }
    normalize_tool_fields(data)
    return data


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("rg pattern web/dist/assets", False),
        ("rg pattern custom-output", False),
        ("rg pattern node_modules/package", False),
        ("rg pattern vendor/lib", False),
        ("rg pattern dist/assets", False),
        ("rg pattern src/build", True),
        ("rg pattern web/dist/assets src", True),
        ("rg pattern web/dist/assets; rg pattern src", True),
        ("rg pattern src; rg pattern web/dist/assets", True),
        ("head -n 40 src/constants.py", False),
        ("head -n 40 src/constants.py; cat src/other.py", True),
        ("gcode outline src/constants.py; cat src/other.py", True),
        ("rg pattern /var/log/system.log", False),
        ("rg pattern /var/log/system.log src", True),
    ],
)
def test_target_exemptions(repo: Path, command: str, blocked: bool) -> None:
    assert navigation_requires_index(event(repo, command), {}) is blocked


@pytest.mark.parametrize(
    "output",
    [
        "file has no indexed symbols in current project: src/constants.py",
        "file type has no AST parser support; `gcode outline` is AST-only: src/constants.py",
        "file not indexed in current project: src/constants.py",
    ],
)
def test_zero_symbol_read_is_file_and_operation_scoped(repo: Path, output: str) -> None:
    result = event(repo, "gcode outline src/constants.py", output)
    variables = {"code_index_recoveries": result["canonical_code_index_recovery"]}
    assert not navigation_requires_index(event(repo, "cat src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "cat src/other.py"), variables)
    assert navigation_requires_index(event(repo, "rg VALUE src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "cat src/constants.py src/other.py"), variables)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "No results.",
        "warning: gcode index refresh already running; reading existing index\nsrc/constants.py:1:VALUE",
    ],
)
def test_search_miss_and_usable_refresh_do_not_grant_recovery(repo: Path, output: str) -> None:
    result = event(repo, "gcode grep VALUE src", output)
    assert not result["canonical_code_index_recovery"]
    assert navigation_requires_index(event(repo, "rg VALUE src"), {})


def test_error_fallback_preserves_search_scope(repo: Path) -> None:
    result = event(repo, "gcode grep 'query/with.dot' src", failed=True)
    variables = {"code_index_recoveries": result["canonical_code_index_recovery"]}
    assert not navigation_requires_index(event(repo, "rg VALUE src/constants.py"), variables)
    assert navigation_requires_index(event(repo, "rg VALUE ."), variables)
    assert navigation_requires_index(event(repo, "cat src/constants.py"), variables)


def test_external_project_error_does_not_open_current_checkout(repo: Path) -> None:
    result = event(repo, "gcode --project /external/archive grep VALUE src", failed=True)
    variables = {"code_index_recoveries": result["canonical_code_index_recovery"]}
    assert navigation_requires_index(event(repo, "rg VALUE src"), variables)


def test_same_turn_write_exemption_is_per_segment(repo: Path) -> None:
    variables = {"turn_written_paths": [str(repo / "src/constants.py")]}
    assert not navigation_requires_index(event(repo, "cat src/constants.py"), variables)
    assert navigation_requires_index(
        event(repo, "cat src/constants.py; cat src/other.py"), variables
    )


def test_ignored_allowlisted_targets_remain_indexed(repo: Path) -> None:
    (repo / ".gobby").mkdir()
    (repo / ".gobby/gcode.json").write_text(
        '{"index":{"hidden_allowlist":["custom-output/**/*.py"]}}'
    )
    assert navigation_requires_index(event(repo, "rg VALUE custom-output"), {})


def test_outline_signature_cannot_forge_empty_diagnostic(repo: Path) -> None:
    result = event(
        repo,
        "gcode outline src/constants.py",
        'src/constants.py:1-3 [function] f sig=def f(x="file has no indexed symbols in current project:")',
    )
    assert not result["canonical_code_index_recovery"]


def test_linked_checkout_generated_paths_remain_accessible(repo: Path) -> None:
    linked = repo.parent / "linked"
    linked.mkdir()
    gitdir = repo / ".git/worktrees/linked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (linked / ".git").write_text(f"gitdir: {gitdir}\n")
    assert not navigation_requires_index(event(repo, f"rg VALUE {linked}/dist/assets"), {})
    assert navigation_requires_index(event(repo, f"rg VALUE {linked}/src"), {})
