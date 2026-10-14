"""Git commit command parsing characterization tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.workflows.git_commit_parsing import parse_git_commit_invocations
from tests.workflows.test_commit_guard import _git

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("command", "nested"),
    [
        ("git -C /wt/agent commit -m x -- file.txt", False),
        ("bash -c 'git -C /wt/agent commit -m x -- file.txt'", True),
        ("sh -c \"bash -c 'git -C /wt/agent commit -m x -- file.txt'\"", True),
    ],
)
def test_parse_git_commit_invocations_handles_chdir_and_nested_shells(
    command: str, nested: bool
) -> None:
    from gobby.workflows.git_commit_parsing import resolve_commit_inspect_cwd

    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    if nested:
        assert invocations[0].cwd_unverified
        assert invocations[0].pathspecs == ()
    else:
        assert invocations[0].chdir == "/wt/agent"
        assert invocations[0].pathspecs == ("file.txt",)
    assert resolve_commit_inspect_cwd(invocations[0], event_cwd="/main", project_path="/main") == (
        "/main" if nested else "/wt/agent"
    )


@pytest.mark.parametrize(
    ("case", "command", "expected"),
    [
        (
            "heredoc-quoted-tag",
            "git commit -F - <<'MSG'\nfix: thing\n\nthe row -- abandonment alike\n"
            "audit / quality audit\nMSG\n",
            [()],
        ),
        (
            "heredoc-unquoted-tag",
            "git commit -F - <<MSG\nfix: thing\n\nthe row -- abandonment alike\n"
            "audit / quality audit\nMSG\n",
            [()],
        ),
        ("heredoc-dash-variant", "git commit -F - <<-MSG\n\tbody -- src/a.py\n\tMSG\n", [()]),
        (
            "heredoc-then-path-scoped-commit",
            "git commit -F - <<'MSG'\nbody -- x/y\nMSG\ngit commit -- src/a.py",
            [(), ("src/a.py",)],
        ),
    ],
    ids=["quoted-tag", "unquoted-tag", "dash-variant", "chained"],
)
def test_parse_ignores_heredoc_bodies(
    case: str, command: str, expected: list[tuple[str, ...]]
) -> None:
    """A heredoc body is unquoted text; `--` in a commit message is not a pathspec delimiter."""
    assert [i.pathspecs for i in parse_git_commit_invocations(command)] == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit -m x\ngit commit -- src/a.py", [(), ("src/a.py",)]),
        ("git commit -m x ; git commit -- src/a.py", [(), ("src/a.py",)]),
        ("git commit -m x && git commit -- src/a.py", [(), ("src/a.py",)]),
        ("git commit -m x\ngit commit -m y", [(), ()]),
        ("git commit \\\n  -- src/a.py", [("src/a.py",)]),
    ],
    ids=["newline", "semicolon", "andand", "two-unscoped", "line-continuation"],
)
def test_parse_separates_newline_chained_commits(
    command: str, expected: list[tuple[str, ...]]
) -> None:
    """A merged parse would inherit the later pathspecs and skip the unscoped commit's check."""
    assert [i.pathspecs for i in parse_git_commit_invocations(command)] == expected


@pytest.mark.parametrize(
    "command",
    [
        'git commit -- "$(cat files)"',
        'git commit -- "$F"',
        'eval "cd /x"; git commit',
        'eval "$(printf cd) /x"; git commit',
        "cat <<EOF\n$(git commit)\nEOF",
    ],
)
def test_parse_unproven_expansion_commit_targets(command: str) -> None:
    invocations = parse_git_commit_invocations(command)
    assert invocations
    assert all(invocation.cwd_unverified for invocation in invocations)


@pytest.mark.parametrize(
    "command",
    [
        "git commit --only -m 'literal' -- 'owned.txt'",
        "cat <<'EOF'\n$(git commit -- foreign.txt)\nEOF",
    ],
)
def test_parse_pattern_guard_preserves_literal_quoted_data(command: str) -> None:
    invocations = parse_git_commit_invocations(command)
    assert all(not invocation.cwd_unverified for invocation in invocations)
    if command.startswith("git"):
        assert len(invocations) == 1
        assert invocations[0].pathspecs == ("owned.txt",)
    else:
        assert invocations == ()


@pytest.mark.parametrize("shell,expected_head", [("bash", "prefix change"), ("zsh", "base owned")])
def test_malformed_later_line_cannot_hide_executed_nested_commit(
    sample_git_project: dict[str, Any], shell: str, expected_head: str
) -> None:
    repo = Path(sample_git_project["repo_path"])
    (repo / "owned.txt").write_text("base owned\n", encoding="utf-8")
    _git(repo, "add", "owned.txt")
    _git(repo, "commit", "-m", "base owned")
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    (repo / "owned.txt").write_text("prefix change\n", encoding="utf-8")
    command = "bash -c 'git commit --only -m prefix -- owned.txt'\nprintf '"
    result = subprocess.run([shell, "-c", command], cwd=repo, capture_output=True, check=False)
    assert result.returncode != 0
    assert _git(repo, "show", "HEAD:owned.txt") == expected_head
    invocations = parse_git_commit_invocations(command)
    assert invocations
    assert all(invocation.cwd_unverified for invocation in invocations)


@pytest.mark.parametrize(
    "option",
    [
        "-m",
        "--message",
        "-F",
        "--file",
        "-C",
        "-c",
        "--reuse-message",
        "--reedit-message",
        "--author",
        "--date",
        "--fixup",
        "--squash",
        "-t",
        "--template",
        "--cleanup",
        "--trailer",
    ],
)
def test_parse_commit_option_values_are_not_pathspecs(option: str) -> None:
    invocations = parse_git_commit_invocations(f"git commit {option} '{{a,b}}' owned.txt")
    assert len(invocations) == 1
    assert invocations[0].pathspecs == ("owned.txt",)
    assert not invocations[0].cwd_unverified


@pytest.mark.parametrize("option", ["--unknown", "--pathspec-from-file=files", "-Z"])
def test_parse_unknown_commit_option_shapes_are_unverified(option: str) -> None:
    invocations = parse_git_commit_invocations(f"git commit {option} owned.txt")
    assert invocations
    assert all(invocation.cwd_unverified for invocation in invocations)


@pytest.mark.parametrize(
    "command,paths",
    [
        ("git commit -m x > commit.log", ()),
        ("git commit -m x < message.txt", ()),
        ("git commit -m x 2>&1", ()),
        ("git commit -m x owned.txt > commit.log", ("owned.txt",)),
        ("git commit -m x -- owned.txt < message.txt", ("owned.txt",)),
        ("git > commit.log commit -m x owned.txt", ("owned.txt",)),
        ("git commit -m x -- '>'", (">",)),
        ("git commit -m x -- '<'", ("<",)),
    ],
)
def test_parse_commit_redirections_are_not_pathspecs(command: str, paths: tuple[str, ...]) -> None:
    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].pathspecs == paths
    assert not invocations[0].cwd_unverified


def test_parse_keeps_explicit_pathspecs_and_quoted_messages() -> None:
    assert [
        i.pathspecs for i in parse_git_commit_invocations("git commit -- src/a.py tests/b.py")
    ] == [("src/a.py", "tests/b.py")]
    assert [
        i.pathspecs for i in parse_git_commit_invocations("git commit -m 'row -- a/b and c/d'")
    ] == [()]


def test_parse_captures_git_c_and_work_tree() -> None:
    from gobby.workflows.git_commit_parsing import resolve_commit_inspect_cwd

    dash_c = parse_git_commit_invocations("git -C /wt/agent commit -m x")
    assert len(dash_c) == 1
    assert dash_c[0].chdir == "/wt/agent"
    assert dash_c[0].work_tree is None
    assert (
        resolve_commit_inspect_cwd(dash_c[0], event_cwd="/main", project_path="/main")
        == "/wt/agent"
    )

    work_tree = parse_git_commit_invocations(
        "git --work-tree=/wt/agent --git-dir=/main/.git commit -m x"
    )
    assert work_tree[0].work_tree == "/wt/agent"
    assert work_tree[0].root_options == (
        "--work-tree=/wt/agent",
        "--git-dir=/main/.git",
    )
    assert (
        resolve_commit_inspect_cwd(work_tree[0], event_cwd="/main", project_path="/main")
        == "/wt/agent"
    )

    relative = parse_git_commit_invocations("git -C ../agent-wt commit -m x")
    assert (
        resolve_commit_inspect_cwd(
            relative[0], event_cwd="/repos/gobby", project_path="/repos/gobby"
        )
        == "/repos/agent-wt"
    )

    bare = parse_git_commit_invocations("git commit -m x")[0]
    assert (
        resolve_commit_inspect_cwd(bare, event_cwd="/wt/agent", project_path="/main") == "/wt/agent"
    )
    assert resolve_commit_inspect_cwd(bare, event_cwd=None, project_path="/main") == "/main"

    unrelated_options = parse_git_commit_invocations(
        "git --exec-path=/tmp -c advice.detachedHead=false commit -m x"
    )[0]
    assert unrelated_options.root_options == ()


def test_shell_cd_combines_relative_navigation_with_git_c() -> None:
    from gobby.workflows.git_commit_parsing import resolve_commit_inspect_cwd

    invocations = parse_git_commit_invocations(
        "cd ../checkouts && cd 'agent tree'; git -C nested commit -m 'keep ; literal' -- file.txt"
    )

    assert len(invocations) == 1
    assert invocations[0].pathspecs == ("file.txt",)
    assert (
        resolve_commit_inspect_cwd(
            invocations[0], event_cwd="/repos/main", project_path="/repos/main"
        )
        == "/repos/checkouts/agent tree/nested"
    )


@pytest.mark.parametrize(
    ("command", "expected_chdir"),
    [
        ("true && cd /owned && git commit -m safe", "/owned"),
        (
            "cd /repos && cd 'agent tree' && git -C nested commit -m safe",
            "/repos/agent tree/nested",
        ),
        ("cd /owned; git commit -m safe", "/owned"),
        ("cd owned && git commit -m 'fix (thing) `x`' -- a.txt", "owned"),
        (
            "git commit -m \"$(cat <<'EOF'\nfix: git commit guard (don't regress)\nEOF\n)\"",
            None,
        ),
        ("bash -c 'echo ready' && git commit -m safe", None),
        ("cd owned && git commit -m safe # (cd /other; see notes)\n", "owned"),
        ('echo "$(date)" && git commit -m safe', None),
    ],
)
def test_conditional_navigation_preserves_literal_commit_paths(
    command: str, expected_chdir: str
) -> None:
    invocations = parse_git_commit_invocations(command)

    assert len(invocations) == 1
    assert not invocations[0].cwd_unverified
    assert invocations[0].chdir == expected_chdir


@pytest.mark.parametrize("continuation", ["\n", " # c\n", "\n\n"])
def test_background_newline_preserves_commit_directory(continuation: str) -> None:
    single_line = parse_git_commit_invocations("cd sub & git commit -m x")
    continued = parse_git_commit_invocations(f"cd sub &{continuation} git commit -m x")

    assert len(single_line) == len(continued) == 1
    assert continued[0].chdir == single_line[0].chdir is None
    assert continued[0].cwd_unverified == single_line[0].cwd_unverified is False


@pytest.mark.parametrize(
    ("command", "contains_commit"),
    [
        ("echo 'unterminated", False),
        ("legit 'unterminated", False),
        ("git commit -m x\necho 'unterminated", True),
        ("g\\it commit -m x\necho 'unterminated", True),
        ("'g'it commit -m x\necho 'unterminated", True),
        ('g"it" commit -m x\necho \'unterminated', True),
    ],
)
def test_malformed_shell_requires_git_word(command: str, contains_commit: bool) -> None:
    invocations = parse_git_commit_invocations(command)
    assert bool(invocations) is contains_commit
    if contains_commit:
        assert all(invocation.cwd_unverified for invocation in invocations)


@pytest.mark.parametrize(
    "command",
    [
        "git -c core.worktree=/foreign commit -m x owned.txt",
        "git -ccore.worktree=/foreign commit -m x owned.txt",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.worktree GIT_CONFIG_VALUE_0=/foreign git commit -m x owned.txt",
        "GIT_CONFIG_PARAMETERS=\"'core.worktree=/foreign'\" git commit -m x owned.txt",
        "export GIT_CONFIG_COUNT=1; git commit -m x owned.txt",
        "declare -x GIT_CONFIG_KEY_0=core.worktree; git commit -m x owned.txt",
        "eval 'export GIT_CONFIG_VALUE_0=/foreign'; git commit -m x owned.txt",
        "n=0; export GIT_CONFIG_KEY_$n=core.worktree; git commit -m x owned.txt",
    ],
)
def test_config_selectors_are_unverified(command: str) -> None:
    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified
