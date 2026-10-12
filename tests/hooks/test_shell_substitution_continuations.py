"""Continuation-insertion corpus retained from the independent shell guard review."""

from pathlib import Path

import pytest

from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.engine.command_matching import command_patterns_match

pytestmark = pytest.mark.unit

GEM_PUSH = r"(^|(?<=[;&|(`\n]))\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*gem\s+push\b"
SEEDS = [
    "cat <(touch a.py)",
    "echo $(touch a.py)",
    "tee >(touch a.py)",
    "cat 2<(touch a.py)",
    "echo hi >a.py",
    "echo hi >>a.py",
    "echo hi 2>a.py",
    "echo hi &>a.py",
    "echo hi 2>&1 >a.py",
    "echo hi >&2a.py",
    "true && touch a.py",
    "true || touch a.py",
    "true; touch a.py",
    "true | tee a.py",
    "cat <<< x; gem push x",
    "cat <<< x && gem push x",
    "true & gem push x",
    "x=$(gem push x)",
    "cat <(gem push x)",
    "echo >(gem push x)",
    "f() { touch a.py; }; f",
    "echo hi >| a.py",
    "(touch a.py)",
    "{ touch a.py; }",
    "echo hi 1>a.py",
    "exec 3>a.py",
    "echo $((1+2)) >a.py",
    "true |& gem push x",
    "echo `touch a.py`",
    'echo "$(touch a.py)"',
    "cat 2<(gem push x)",
    "echo $(gem push x)",
    "cp b.py a.py",
]


def _outcome(command: str, root: Path) -> tuple[object, object, tuple[bool, bool]]:
    metadata = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command, "cwd": str(root)},
            "project_path": str(root),
        }
    )
    return (
        metadata.get("canonical_repo_mutation"),
        metadata.get("canonical_file_paths"),
        (
            command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=False),
            command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=True),
        ),
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_continuation_insertion_preserves_shell_guards(seed: str, tmp_path: Path) -> None:
    expected = _outcome(seed, tmp_path)
    for position in range(len(seed) + 1):
        if position and seed[position - 1] == "\\":
            continue
        for insertion in ("\\\n", "\\\n\\\n"):
            command = seed[:position] + insertion + seed[position:]
            assert _outcome(command, tmp_path) == expected, repr(command)


def test_continuation_corpus_covers_1160_variants() -> None:
    count = sum(
        2
        for seed in SEEDS
        for position in range(len(seed) + 1)
        if not position or seed[position - 1] != "\\"
    )
    assert count == 1160
