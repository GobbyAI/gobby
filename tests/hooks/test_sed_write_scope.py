"""Sed writes retain their destination scope alongside read input paths."""

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


@pytest.mark.parametrize("program", ["s/a/b/w out.txt", "w out.txt", "W out.txt"])
@pytest.mark.parametrize("option", ["", "-e ", "--expression="])
def test_inline_sed_write_reports_destination(tmp_path: Path, program: str, option: str) -> None:
    data = _metadata(f"sed -n {option}'{program}' in.py", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == ["out.txt"]
    assert data["canonical_file_paths"] == ["out.txt"]
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("option", ["-f ", "--file="])
def test_file_sed_script_write_reports_destination(tmp_path: Path, option: str) -> None:
    script = tmp_path / "write.sed"
    script.write_text("1,5w out.txt\n", encoding="utf-8")
    data = _metadata(f"sed {option}{script} in.py", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == ["out.txt"]
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_unreadable_sed_script_cannot_prove_read_only_scope(tmp_path: Path) -> None:
    data = _metadata("sed -f missing.sed in.py", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "program",
    [
        "1,5p",
        "s/a/b/",
        "10,$p",
        "s/w out.txt/replacement/",
        "s/a/w out.txt/",
        "a w out.txt",
        "# w out.txt",
        "/w out.txt/p",
        "y/w/W/",
    ],
)
def test_read_only_sed_program_stays_read(tmp_path: Path, program: str) -> None:
    data = _metadata(f"sed -n '{program}' f", tmp_path)
    assert data["canonical_tool_kind"] == "read"
    assert not data.get("canonical_repo_mutation")


@pytest.mark.parametrize("program", ["1,5w out.txt", "/start/,$w out.txt", "s#a#b#gpw out.txt"])
def test_addressed_and_alternate_delimiter_writes(tmp_path: Path, program: str) -> None:
    data = _metadata(f"sed '{program}' in.py", tmp_path)
    assert data["canonical_write_file_paths"] == ["out.txt"]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("flag", ["-i", "-I .bak", "-ni"])
def test_sed_in_place_and_program_write_keep_both_destinations(tmp_path: Path, flag: str) -> None:
    data = _metadata(f"sed {flag} 's/a/b/w out.txt' in.py", tmp_path)
    assert data["canonical_write_file_paths"] == ["in.py", "out.txt"]


def test_multiple_sed_programs_keep_all_destinations(tmp_path: Path) -> None:
    data = _metadata("sed -e 'w first.txt' -e 'w second.txt' in.py", tmp_path)
    assert data["canonical_write_file_paths"] == ["first.txt", "second.txt"]


def test_relative_sed_script_resolves_after_absolute_cd(tmp_path: Path) -> None:
    directory = tmp_path / "sub"
    directory.mkdir()
    (directory / "write.sed").write_text("w out.txt\n", encoding="utf-8")
    data = _metadata(f"cd {directory}; sed -f write.sed in.py", tmp_path)
    assert data["canonical_write_file_paths"] == [str(directory / "out.txt")]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("program", ["w out.txt", "W out.txt", "s/a/b/w out.txt"])
def test_sed_pipeline_filter_still_reports_writes(tmp_path: Path, program: str) -> None:
    data = _metadata(f"cat in.py | sed '{program}'", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == ["out.txt"]


def test_unreadable_sed_pipeline_script_keeps_unknown_scope(tmp_path: Path) -> None:
    data = _metadata("cat in.py | sed -f missing.sed", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("program", ["e touch out.txt", "s/a/b/e", "w"])
def test_unproven_sed_program_keeps_unknown_write_scope(tmp_path: Path, program: str) -> None:
    data = _metadata(f"sed '{program}' in.py", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_sed_write_path_can_contain_a_semicolon(tmp_path: Path) -> None:
    data = _metadata("sed 'w out.txt; p' in.py", tmp_path)
    assert data["canonical_write_file_paths"] == ["out.txt; p"]


def test_sed_scratch_write_does_not_mutate_read_input(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    destination = tmp_path / "out.txt"
    data = _metadata(f"sed 'w {destination}' in.py", root)
    assert data["canonical_write_file_paths"] == [str(destination)]
    assert data["canonical_repo_mutation"] is False
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_sed_append_text_escaped_backslash_does_not_hide_following_write(tmp_path: Path) -> None:
    program = "a\\\ntext\\\\\nw out.txt"
    data = _metadata(f"sed '{program}' in.py", tmp_path)
    assert data["canonical_write_file_paths"] == ["out.txt"]


def test_known_scratch_destination_cannot_hide_unproven_sed_program(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    data = _metadata(f"sed -e 'w {tmp_path / 'out.txt'}' -e 's/a/b/e' in.py", root)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
