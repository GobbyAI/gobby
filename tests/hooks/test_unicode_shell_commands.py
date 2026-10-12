"""Regression coverage for the #24142 close-review Bash probe."""

import pytest

from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.commit_guard import parse_git_commit_invocations
from gobby.workflows.engine.command_matching import (
    command_patterns_match,
    executable_command_subjects,
)

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
