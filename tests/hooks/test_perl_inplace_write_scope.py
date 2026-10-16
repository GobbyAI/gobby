"""Perl in-place edits write their file operands; perl without -i stays a non-write."""

from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit


def _metadata(command: str, root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(root)},
        "project_path": str(root),
    }
    normalize_tool_fields(data)
    return data


@pytest.mark.parametrize(
    ("command", "written"),
    [
        ("perl -pi -e 's/a/b/' src/x.py", ["src/x.py"]),
        ("perl -i.bak -pe 's/a/b/' src/x.py", ["src/x.py"]),
        ("perl -pi.orig -e 's/a/b/' src/x.py", ["src/x.py"]),
        ("perl -0pi -e 's/a/b/' crates/gdaemon/tests/nodes.rs", ["crates/gdaemon/tests/nodes.rs"]),
        ("perl -0777 -pi -e 's/a/b/' a.py b.py", ["a.py", "b.py"]),
        ("perl -i -pe 's/a/b/' a.py b.py", ["a.py", "b.py"]),
        ("perl -i -p -e 's/a/b/' -- a.py", ["a.py"]),
        ("perl -I lib -Mstrict -pi -e 's/a/b/' a.py", ["a.py"]),
        ("perl -i -p fix.pl a.py b.py", ["a.py", "b.py"]),
    ],
)
def test_perl_in_place_edit_writes_its_file_operands(
    tmp_path: Path, command: str, written: list[str]
) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == written
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command",
    [
        "perl -ne 'print' src/x.py",
        "perl -pe 's/a/b/' src/x.py",
        "perl -e 'print \"-i\"' src/x.py",
        "perl fix.pl -i src/x.py",
    ],
)
def test_perl_without_in_place_switch_is_not_a_write(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] != "write"
    assert not data.get("canonical_write_file_paths")
