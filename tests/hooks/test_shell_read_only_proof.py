"""Missing native cwd can be tolerated only for a whole-program read-only proof."""

import subprocess
import sys
from pathlib import Path

import pytest

from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("command", "read_only"),
    [
        ("git status --short", True),
        ("git log -1", True),
        ("git diff --stat", True),
        ("git diff --output=out.txt", False),
        ("git diff --ext-diff", False),
        ("git show HEAD:file.py", True),
        ("git merge HEAD", False),
        ("git reset --hard", False),
        ("rg -n pattern file.py", True),
        ("rg --pre ./preprocess pattern file.py", False),
        ("rg --pre=./preprocess pattern file.py", False),
        ("cat file.py | rg --pre ./preprocess pattern", False),
        ("gcode outline file.py", True),
        ("cat file.py", True),
        ("bat file.py", True),
        ("bat --paging=always --pager=./pager file.py", False),
        ("bat --generate-config", False),
        ("less -o saved.txt file.py", False),
        ("pwd", True),
        ("ls -l", True),
        ("uv run --no-sync ruff check file.py", False),
        ("ruff check --output-file out.txt file.py", False),
        ("ruff check --add-noqa file.py", False),
        ("uv run --no-sync ruff format --diff file.py", True),
        ("ruff format -- --check file.py", False),
        ("ruff format -- --diff file.py", False),
        ("ruff format --config --check file.py", False),
        ("patch --dry-run < changes.patch", True),
        ("patch -- --dry-run < changes.patch", False),
        ("patch -i --dry-run file.py", False),
        ("uv run --no-sync ruff format file.py", False),
        ("uv run --no-sync ruff check --fix file.py", False),
        ("uv run python -c 'print(1)'", True),
        ("uv run python -c 'unknown()'", False),
        ('uv run python -c \'open("file.py", "w").write("x")\'', False),
        ("python - <<'PY'\nprint(1)\nPY", True),
        ("python - <<'PY'\nunknown()\nPY", False),
        ("bash -n script.sh", True),
        ("bash script.sh", False),
        ("bash -c 'cat file.py; unknown-program'", False),
        ("cat file.py; unknown-program", False),
        ("cat file.py; touch other.py", False),
        ("cat file.py | wc -l", True),
        ("cat file.py | sort", True),
        ("cat file.py | sort --compress-program=./compressor", False),
        ("cat file.py | sort --compress-prog=./compressor", False),
        ("cat file.py | jq .", True),
        ("cat file.py | uniq - out.txt", False),
        ("cat file.py | uniq --skip-f 1 - out.txt", False),
        ("cat file.py | uniq - -", True),
        ("cd . > out.txt", False),
        ("cat file.py | unknown-program", False),
        ("echo $(unknown-program)", False),
        ("echo $(git status --short)", True),
        ("cat <(unknown-program)", False),
        ("cat file.py > other.py", False),
        ("find . -type f", True),
        ("find . -delete", False),
        ("find . -exec touch out.txt ';'", False),
        ("touch file.py", False),
        ("unknown-program", False),
    ],
)
def test_whole_shell_read_only_proof(command: str, read_only: bool) -> None:
    data = normalize_tool_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert data.get("canonical_read_only", False) is read_only


def test_nested_script_keeps_unknown_execution_in_read_only_proof(tmp_path: Path) -> None:
    script = tmp_path / "script.sh"
    script.write_text("cat file.py; unknown-program\n")
    data = normalize_tool_fields(
        {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "bash script.sh"}}
    )
    assert data["canonical_tool_kind"] == "read"
    assert data["canonical_read_only"] is False


def test_ruff_check_can_rewrite_from_workdir_configuration(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nfix = true\n")
    source = tmp_path / "file.py"
    source.write_text("import os\n")
    data = normalize_tool_fields(
        {"tool_name": "Bash", "tool_input": {"command": "ruff check file.py"}}
    )
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "file.py"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    assert b"1 fixed" in result.stdout
    assert source.read_text() == ""
    assert data["canonical_read_only"] is False


@pytest.mark.parametrize("filename", ["--check", "--diff"])
def test_ruff_format_option_like_filename_still_writes(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("x=1\n")
    source = tmp_path / "file.py"
    source.write_text("x=1\n")
    data = normalize_tool_fields(
        {"tool_name": "Bash", "tool_input": {"command": f"ruff format -- {filename} file.py"}}
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--", filename, "file.py"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    assert source.read_text() == "x = 1\n"
    assert data["canonical_read_only"] is False
