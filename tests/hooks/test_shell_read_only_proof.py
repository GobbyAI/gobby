"""Missing native cwd can be tolerated only for a whole-program read-only proof."""

import os
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
        ("uv run python -c 'print(1)'", False),
        ("uv run --frozen python -c 'print(1)'", True),
        ("uv run --locked python -c 'print(1)'", True),
        ("uv run --no-sync python -c 'print(1)'", True),
        ("uv run -U python -c 'print(1)'", False),
        ("uv run --upgrade python -c 'print(1)'", False),
        ("uv run --locked -U python -c 'print(1)'", False),
        ("uv run --frozen --upgrade-package example python -c 'print(1)'", False),
        ("uv run --frozen --env-file .env python -c 'print(1)'", False),
        ("uv run --package --frozen python -c 'print(1)'", False),
        ("uv run ruff format --diff file.py", False),
        ("cat file.py | uv run python -c 'print(1)'", False),
        ("uv run python - <<'PY'\nprint(1)\nPY", False),
        ("uv run --frozen python - <<'PY'\nprint(1)\nPY", True),
        ("uv run --frozen uv run python -c 'print(1)'", False),
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
        ("find . -fprint out.txt", False),
        ("find . -fprint0 out.txt", False),
        ("find . -fls out.txt", False),
        ("find . -fprintf out.txt '%p'", False),
        ("find . -exec touch out.txt ';'", False),
        ("touch file.py", False),
        ("unknown-program", False),
    ],
)
def test_whole_shell_read_only_proof(command: str, read_only: bool) -> None:
    data = normalize_tool_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert data.get("canonical_read_only", False) is read_only


@pytest.mark.parametrize(
    ("command", "read_only"),
    [
        ("GIT_EXTERNAL_DIFF=./x git diff", False),
        ("env GIT_EXTERNAL_DIFF=./x git diff", False),
        (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external GIT_CONFIG_VALUE_0=./x git diff",
            False,
        ),
        (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=./x git status",
            False,
        ),
        ("GIT_TRACE=out.txt git log -1", False),
        ("RIPGREP_CONFIG_PATH=./cfg rg x", False),
        ('BASH_ENV=./x bash -c "cat f"', False),
        ("LD_PRELOAD=./x.so cat f", False),
        ("env -S 'LD_PRELOAD=./x.so cat f'", False),
        ("env -- LANG=C LD_PRELOAD=./x.so cat f", False),
        ("cat f | LD_PRELOAD=./x.so wc -l", False),
        ("LD_PRELOAD=./x.so python - <<'PY'\nprint(1)\nPY", False),
        ("uv run --frozen env GIT_TRACE=out.txt git log -1", False),
        ("LANG=C LD_PRELOAD=./x.so gcode outline f", False),
        ("GIT_TRACE=out.txt; git log -1", False),
        ("LANG=C git status --short", True),
        ("env LANG=C LC_ALL=C cat f", True),
        ("cat f | env LC_ALL=C sort", True),
        ("echo GIT_TRACE=out.txt", True),
    ],
)
def test_environment_participates_in_read_only_proof(command: str, read_only: bool) -> None:
    data = normalize_tool_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert data.get("canonical_read_only", False) is read_only


@pytest.mark.parametrize("flags", [[], ["--frozen"], ["--locked"], ["--no-sync"], ["-U"]])
def test_uv_run_lockfile_effects(tmp_path: Path, flags: list[str]) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "proof-fixture"\nversion = "0.1.0"\n')
    environment = {
        **{key: value for key, value in os.environ.items() if not key.startswith("UV_")},
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / ".venv"),
        "UV_CACHE_DIR": str(tmp_path / "cache"),
        "UV_PYTHON": sys.executable,
    }
    lock = subprocess.run(
        ["uv", "lock", "--offline", "--no-config", "--no-python-downloads"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lock.returncode == 0, lock.stderr
    lockfile = tmp_path / "uv.lock"
    before = lockfile.read_bytes()
    project.write_text('[project]\nname = "proof-fixture"\nversion = "0.2.0"\n')
    run = subprocess.run(
        [
            "uv",
            "run",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            *flags,
            "python",
            "-c",
            "print(1)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if flags == ["--locked"]:
        assert run.returncode != 0
        assert "lockfile" in run.stderr
    else:
        assert run.returncode == 0, run.stderr
    assert (lockfile.read_bytes() != before) is (flags in ([], ["-U"]))


@pytest.mark.parametrize("via_environment", [False, True])
def test_uv_env_file_can_change_nested_runner_directory(
    tmp_path: Path, via_environment: bool
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    env_file = first / ".env"
    env_file.write_text(f"UV_WORKING_DIR={second}\n")
    environment = {
        **{key: value for key, value in os.environ.items() if not key.startswith("UV_")},
        "UV_CACHE_DIR": str(tmp_path / "cache"),
    }
    flags = [] if via_environment else ["--env-file", str(env_file)]
    if via_environment:
        environment["UV_ENV_FILE"] = str(env_file)
    runner = ["uv", "run", "--no-project", "--no-sync", "--offline", "--no-config"]
    run = subprocess.run(
        [*runner, *flags, *runner, sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=first,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert Path(run.stdout.strip()) == second


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
