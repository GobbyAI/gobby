"""Regression coverage for the #24142 close-review Bash probe."""

import pytest

from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.engine.command_matching import (
    command_patterns_match,
    executable_command_subjects,
)
from gobby.workflows.git_commit_parsing import parse_git_commit_invocations

pytestmark = pytest.mark.unit

# Recovered from gobby#16470, transcript source line 126.
ORIGINAL_COMMAND = r"""uv run python -c '
import re, hashlib, subprocess
text = open(".gobby/plans/research/closed-unlanded-audit-2026-10.md", encoding="utf-8").read()
pin = "4ccb3e966ed18fe13cd9f268d55143128d034048"
pat = re.compile(r"^\| ([A-Z]\d+) \| [`]?([^|`]+?)[`]? \| (\d+)[–-](\d+) \| [`]([a-f0-9]{64})[`] \|$", re.M)
rows = pat.findall(text)
print("evidence rows", len(rows))
cache = {}
bad = []
for name, path, a, b, h in rows:
    if path not in cache:
        r = subprocess.run(["git", "show", pin + ":" + path], capture_output=True)
        cache[path] = r.stdout if r.returncode == 0 else None
    blob = cache[path]
    if blob is None:
        bad.append((name, path, "missing blob")); continue
    excerpt = b"".join(blob.splitlines(keepends=True)[int(a) - 1:int(b)])
    if hashlib.sha256(excerpt).hexdigest() != h:
        bad.append((name, path, a, b))
print("mismatches", len(bad))
for x in bad[:20]:
    print(x)
'"""


@pytest.mark.parametrize(
    "command,kind",
    [
        pytest.param(ORIGINAL_COMMAND, "execute", id="original-review-probe"),
        pytest.param(ORIGINAL_COMMAND.replace("–", "-"), "execute", id="ascii-original"),
        pytest.param("cat 'audit–excerpt.md'", "read", id="minimal-en-dash"),
        pytest.param("echo '– `git commit` $(git commit)'", "execute", id="literal-data"),
        pytest.param("echo '`[x]+?`'", "execute", id="ascii-backtick-data"),
    ],
)
def test_unicode_bash_probe_normalizes_and_matches(command: str, kind: str) -> None:
    assert parse_git_commit_invocations(command) == ()
    assert executable_command_subjects(command) == [command]
    assert not command_patterns_match(command, pattern=r"\btouch\b", mask_quoted=True)
    assert not command_patterns_match(command, pattern=r"\btouch\b", mask_quoted=False)
    data = normalize_tool_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert data["canonical_tool_kind"] == kind


@pytest.mark.parametrize(
    "command",
    [
        'echo "– $(git commit -m nested)"',
        'echo "– `git commit -m nested`"',
        "cat <(git commit -m nested)",
        "(git commit -m nested)",
        "bash -c 'git commit -m nested'",
        "echo $(echo $(git commit -m nested))",
    ],
)
def test_real_nested_commit_remains_unverified(command: str) -> None:
    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified is True


@pytest.mark.parametrize(
    "command",
    [
        'git --git-dir "$(pwd)" commit -m x',
        'git --work-tree "$(pwd)" commit -m x',
        'git --work-tree="$(pwd)" commit -m x',
        'git -C "$(pwd)" commit -m x',
        'git -C "`pwd`" commit -m x',
        'git -C="$(pwd)" commit -m x',
        'cd "$(pwd)" && git commit -m x',
        "git -C $(echo sub) commit -m x",
        'git commit -m x -- "$(cat files)"',
        'git commit -m x -- "$F"',
        'eval "cd /x"; git commit -m x -- a.py',
        "eval 'cd /x'; git commit -m x -- a.py",
        'source "$(echo f)"; git commit -m x -- a.py',
        ". script.sh; git commit -m x -- a.py",
    ],
)
def test_dynamic_commit_targets_refuse_before_ownership_lookup(command: str) -> None:
    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified is True


@pytest.mark.parametrize(
    "opening,closing", [("{ ", "; }"), ("if true; then ", "; fi"), ("echo $(", ")")]
)
def test_deep_shell_groups_refuse_without_recursing(opening: str, closing: str) -> None:
    command = opening * 600 + "git commit -m nested" + closing * 600
    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified is True


def test_unquoted_data_heredoc_still_executes_substitutions() -> None:
    invocations = parse_git_commit_invocations("cat <<EOF\n$(git commit -m q)\nEOF")
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified is True


@pytest.mark.parametrize("argument", ['-m "eval"', '-m "source"', "-m x -- ."])
def test_navigation_names_in_commit_data_remain_literal(argument: str) -> None:
    invocations = parse_git_commit_invocations(f"git commit {argument}")
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified is False
