"""How the agent workspace read-scope check parses Bash commands it judges.

Heredoc data, inline interpreter code and jq string arguments are not read targets; a
literal path naming another seat's folder still is.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from gobby.workflows.condition_helpers_sessions import agent_workspace_read_violation
from gobby.workflows.workspace_read_targets import _shell_path_words

pytestmark = pytest.mark.unit

PROJECT = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The workspaces root under a temporary home, outside the OS temp root."""
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GOBBY_HOME", raising=False)
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(os_temp))
    monkeypatch.setattr("gobby.workflows.condition_helpers_paths._PROVIDER_TEMP_ROOT", os_temp)
    return home / ".gobby" / "agents_workspaces"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    project = tmp_path / "checkout"
    project.mkdir()
    return project


def _folders(root: Path) -> tuple[dict[str, Any], Path, Path]:
    """Seat variables, the seat's own session folder, and a sibling seat's folder."""
    own = root / PROJECT / "fixture-worker" / "session-50"
    sibling = root / PROJECT / "fixture-worker" / "session-51"
    variables = {
        "_agent_workspace_dir": str(own),
        "_agent_workspace_epic_dir": str(root / PROJECT / "epics" / "epic-5"),
        "_agent_workspace_persistent": False,
        "_agent_type": "fixture-worker",
    }
    return variables, own, sibling


def _refused(command: str, variables: dict[str, Any], cwd: Path) -> bool:
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
        "project_path": str(cwd),
    }
    return agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


_PROSE = "# Standing directives\nKeep the operator's rules.\n* first bullet\n* second bullet\n"


@pytest.mark.parametrize(
    "header",
    [
        "mkdir -p {own} && cat > {own}/standing-directives.md <<'EOF'",
        'mkdir -p {own} && cat > {own}/standing-directives.md <<"EOF"',
        "mkdir -p {own} && cat > {own}/standing-directives.md <<\\EOF",
        "mkdir -p {own}; cat <<-'EOF' > {own}/standing-directives.md",
        "git add . && git commit -F - <<'EOF'",
    ],
)
def test_quoted_heredoc_prose_in_a_compound_command_is_data(
    root: Path, checkout: Path, header: str
) -> None:
    variables, own, _ = _folders(root)
    command = header.format(own=own) + "\n" + _PROSE + "EOF"

    assert not _refused(command, variables, checkout), command
    # The seat's own folder as cwd: a `* bullet` line is not a glob read of the folder.
    assert not _refused(command, variables, own.parent), command
    assert "*" not in _shell_path_words(command, checkout), command


def test_a_quoted_regex_over_checkout_paths_is_not_a_workspace_read(
    root: Path, checkout: Path
) -> None:
    """A read-only grep that names no workspace path passes (#25578 family)."""
    variables, _, _ = _folders(root)
    command = (
        'grep -rniE "spawned.*(sibling|linked|another checkout|other checkout)'
        '|(sibling|linked) (worktree|checkout).*(spawned|managed)" '
        "docs src/gobby/install/shared/skills/gobby/references/code-index"
    )
    # A task worktree sits beside the workspaces root under the same home.
    worktree = root.parent / "worktrees" / "project" / "task-1"
    worktree.mkdir(parents=True)

    assert not _refused(command, variables, checkout)
    assert not _refused(command, variables, worktree)


def test_unparseable_command_text_does_not_escape_the_check(root: Path, checkout: Path) -> None:
    variables, own, sibling = _folders(root)
    # A stray apostrophe outside any heredoc leaves the command unparseable for the scanner,
    # so every word between operators is judged instead.
    assert not _refused(f"cat {own}/notes.md && echo it's done", variables, checkout)
    assert _refused(f"cat {sibling}/notes.md && echo it's done", variables, checkout)
    # An unquoted heredoc body is shell text, so an apostrophe in it is part of the command.
    command = f"mkdir -p {own} && cat > {own}/notes.md <<EOF\nthe operator's note\nEOF"
    assert not _refused(command, variables, checkout)
    assert _refused(f"{command}\ncat {sibling}/notes.md", variables, checkout)


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF' | python3 -\nprint(open('{sibling}/notes.md').read())\nEOF",
        "mkdir -p {own} && python3 - <<'EOF'\nprint(open('{sibling}/notes.md').read())\nEOF",
        "mkdir -p {own} && bash <<'EOF'\ncat {sibling}/notes.md\nEOF",
        "cd {own} && . /dev/stdin <<'EOF'\ncat {sibling}/notes.md\nEOF",
    ],
)
def test_heredoc_bodies_a_program_reads_keep_foreign_paths_refused(
    root: Path, checkout: Path, command: str
) -> None:
    variables, own, sibling = _folders(root)

    assert _refused(command.format(own=own, sibling=sibling), variables, checkout)


@pytest.mark.parametrize(
    "template",
    [
        "python3 -c \"print(open('{own}/notes.md').read())\"",
        "python -c 'import json; print(json.load(open(\"{own}/state.json\")))'",
        "uv run python -c \"from pathlib import Path; print(Path('{own}/notes.md').read_text())\"",
        "node -e \"console.log(require('fs').readFileSync('{own}/notes.md', 'utf8'))\"",
        "ruby -e \"puts File.read('{own}/notes.md')\"",
    ],
)
def test_inline_interpreter_code_reads_of_the_own_folder_pass(
    root: Path, checkout: Path, template: str
) -> None:
    variables, own, _ = _folders(root)
    command = template.format(own=own)

    assert not _refused(command, variables, checkout), command
    assert not _refused(command, variables, own), command


def test_inline_interpreter_code_operators_are_not_paths(root: Path, checkout: Path) -> None:
    variables, own, _ = _folders(root)
    command = 'python3 -c "import sys; print(2*3/4, [*range(3)], sys.argv[1:])"'

    assert _shell_path_words(command, checkout) == []
    assert not _refused(command, variables, own)


@pytest.mark.parametrize(
    "template",
    [
        "python3 -c \"print(open('{sibling}/notes.md').read())\"",
        "uv run python -c \"import os; print(os.listdir('{sibling}'))\"",
        "python3 -c \"import glob; print(glob.glob('{sibling}/*'))\"",
        "node -e \"console.log(require('fs').readFileSync('{sibling}/notes.md'))\"",
        "ruby -e \"puts File.read('{sibling}/notes.md')\"",
        "python3 -c \"print(open('$(cat {sibling}/path.txt)').read())\"",
    ],
)
def test_inline_interpreter_code_naming_a_foreign_folder_stays_refused(
    root: Path, checkout: Path, template: str
) -> None:
    variables, _, sibling = _folders(root)
    command = template.format(sibling=sibling)

    assert _refused(command, variables, checkout), command


def test_jq_string_arguments_are_not_reads(root: Path, checkout: Path) -> None:
    variables, own, sibling = _folders(root)
    readable = own / "index.json"
    for value in (f"{sibling}/notes.md", f"'{sibling}/notes.md'"):
        command = f"jq --arg p {value} '.[] | select(.path==$p)' {readable}"
        assert not _refused(command, variables, checkout), command
        assert not _refused(command, variables, own), command
    command = f"jq -n --argjson v '{{\"path\": \"{sibling}\"}}' '$v.path'"
    assert not _refused(command, variables, checkout), command


@pytest.mark.parametrize(
    "template",
    [
        "jq --rawfile body {sibling}/notes.md -n '$body'",
        "jq --slurpfile rows {sibling}/rows.json -n '$rows'",
        "jq --arg p x '.' {sibling}/state.json",
        "jq --arg p \"$(cat {sibling}/notes.md)\" -n '$p'",
        "jq -f {sibling}/filter.jq {own}/index.json",
    ],
)
def test_jq_file_operands_stay_judged(root: Path, checkout: Path, template: str) -> None:
    variables, own, sibling = _folders(root)
    command = template.format(own=own, sibling=sibling)

    assert _refused(command, variables, checkout), command
