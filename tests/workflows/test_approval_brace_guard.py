"""Bounded protected-name recognition, independent of launcher position."""

import time

import pytest

from gobby.workflows.approval_brace_guard import (
    approval_brace_spelling,
    approval_command_word_expansion,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        "unknown-launcher g{o..o}bby sessions list",
        "unknown-launcher {f..h}{n..p}{a..c}{a..c}{x..z} sessions list",
        "unknown-launcher {h..f}{p..n}{c..a}{c..a}{z..x} sessions list",
        "unknown-launcher {f..h..1}obby sessions list",
        "unknown-launcher {a..z..6}obby sessions list",
        "unknown-launcher {h..f..1}obby sessions list",
        "unknown-launcher {g..g}obby sessions list",
        "unknown-launcher {gobby,sessions,approve-dirty-paths}",
        "unknown-launcher {g{ob,ar}by,other} sessions list",
        "unknown-launcher .venv/bin/{gobby,other} sessions list",
        "unknown-launcher gobby sessions approve-{dirty-paths,}",
        "unknown-launcher gobby sessions {approve-dirty-paths,other}",
        "unknown-launcher gobby sessions a{p,q}prove-dirty-paths",
        "echo gobb{y..y}",
        "echo 'gob'{b,y}y",
        'echo "gob"{b,y}y',
        "echo g?b{b,y}y",
        "echo g*{b,y}y",
        "sh -c 'unknown-launcher g{o..o}bby sessions list'",
        "eval 'unknown-launcher {gobby,} sessions list'",
        "echo $(unknown-launcher {gobby,} sessions list)",
        "echo ${NAME}{,}",
        "echo g[ao]b{b,y}y",
        "unknown-launcher @(g{obby,})",
        r"unknown-launcher [\!g]{o,b}bby",
        "unknown-launcher []g]{o,b}bby",
        "unknown-launcher [a{b..h}]obby",
        "echo " + "{a," * 40 + "gobby" + "}" * 40,
    ],
)
def test_protected_brace_words_are_refused(command: str) -> None:
    assert approval_brace_spelling(command)


@pytest.mark.parametrize(
    "command",
    [
        "unknown-launcher gobby sessions approve-dirty-paths",
        'unknown-launcher "{gobby,}" sessions "approve-{dirty-paths,}"',
        "unknown-launcher '{gobby,}' sessions 'approve-{dirty-paths,}'",
        r"unknown-launcher \{gobby,\} sessions approve-\{dirty-paths,\}",
        "echo {a,b}",
        "cp f{,.bak}",
        "ls {gobby,x}.md",
        "gobby tasks show {a,b}",
        "gobby sessions list --project {a,b}",
        "pytest tests/{a,b}.py",
        "rg -g *.{py,md} sessions src",
        "rg -g [a-z].{py,md} sessions src",
        "echo {a..f}obby",
        "echo {f..h..2}obby",
        "echo {1..999999999999999999}obby",
        "echo {a,{b,c}}",
        'echo "gobb{y..y}"',
        r"echo gobb\{y..y\}",
        "cat <<'EOF'\ng{o..o}bby\nEOF",
        "echo hello # g{o..o}bby",
    ],
)
def test_ordinary_and_literal_brace_data_stays_allowed(command: str) -> None:
    assert not approval_brace_spelling(command)


def test_large_brace_inputs_do_not_generate_cartesian_expansions() -> None:
    started = time.perf_counter()
    assert not approval_brace_spelling("echo " + "{" * 15000)
    assert not approval_brace_spelling("echo " + "{a,b}" * 15000)
    assert approval_brace_spelling("echo " + "{" * 15000 + "a" + "}" * 15000)
    assert time.perf_counter() - started < 5.0


@pytest.mark.parametrize("launcher", ["caffeinate", "/usr/bin/arch -arch arm64", "unknown"])
@pytest.mark.parametrize("target", ["gobby", "approve-dirty-paths"])
def test_fully_ranged_protected_words_behind_arbitrary_launchers(
    launcher: str, target: str
) -> None:
    ranged = "".join(f"{{{char}..{char}}}" if char.isalpha() else char for char in target)
    assert approval_brace_spelling(f"{launcher} {ranged}")


@pytest.mark.parametrize("word", ["{'g'..'g'}obby", r"{g\..g}obby"])
def test_zsh_ranges_can_use_quoted_or_escaped_range_components(word: str) -> None:
    assert approval_brace_spelling(f"unknown-launcher {word} sessions list")


@pytest.mark.parametrize(
    "command",
    [
        "caffeinate g{o}bby sessions a{p}prove-dirty-paths s --checkout /r a.txt",
        "/usr/bin/arch -arch arm64 g{o}bby sessions list",
        "unknown {g}{o}{b}{b}{y}",
        "unknown {f-h}obby",
        "unknown {abg}obby",
        "unknown x{./}gobby",
        "unknown x{.-0}gobby",
        "echo {{x,y}}",
        "echo {'x'}",
    ],
)
def test_zsh_brace_ccl_groups_are_checked_symbolically(command: str) -> None:
    assert approval_brace_spelling(command)


@pytest.mark.parametrize(
    "command",
    [
        "find . -exec rm {} +",
        "git log HEAD@{u}",
        "echo '{o}'",
        r"echo \{o\}",
        "echo {x}",
        "ls ${HOME}",
        "cd ${WT} && git status",
        "echo ${x:-y}",
        'echo ${x:-"y"}',
    ],
)
def test_brace_ccl_and_parameter_data_controls_stay_allowed(command: str) -> None:
    assert not approval_brace_spelling(command)


@pytest.mark.parametrize("command", ["echo ${NAME}{,}", "echo ${N}obb{y,}"])
def test_parameter_spans_preserve_target_matching_and_ambiguity_refusals(command: str) -> None:
    assert approval_brace_spelling(command)


@pytest.mark.parametrize(
    "command",
    [
        "env " * 1000 + "echo hello",
        "env -S '" + "env " * 1000 + "echo hello'",
    ],
    ids=["deep-argv", "split-string"],
)
def test_deep_literal_wrapper_data_stays_allowed(command: str) -> None:
    assert not approval_command_word_expansion(command)


@pytest.mark.parametrize("command", ["cd ${WT:-${HOME}}", "echo ${A:-$(pwd)}", "echo ${A:-${B}}"])
def test_unrelated_nested_parameter_data_stays_allowed(command: str) -> None:
    assert not approval_command_word_expansion(command)


@pytest.mark.parametrize(
    "command",
    ["gob${x:-${y}}by", "${A:-{gobby,x}}", "{gob,}by sessions approve-dirty-paths"],
)
def test_nested_parameter_protected_words_stay_refused(command: str) -> None:
    assert approval_command_word_expansion(command)
