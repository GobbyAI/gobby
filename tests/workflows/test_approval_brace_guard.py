"""Bounded protected-name recognition, independent of launcher position."""

import time

import pytest

from gobby.workflows.approval_brace_guard import (
    approval_brace_spelling,
    approval_command_word_expansion,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

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


@pytest.mark.parametrize(
    "command",
    [
        "A= B=; g${A:-${B}}obby se${A:-${B}}ssions approve-dirty-pat${A:-${B}}hs s7 --checkout /repo a.txt",
        "g${A:-${B}}obby sessions list",
        "go${A:-${B}}bby sessions list",
        "unknown app${A:-${B}}rove-dirty-paths",
        "g${A}obby se${A}ssions approve-dirty-pat${A}hs",
        "go${A}bby sessions list",
        "unknown app${A}rove-dirty-paths",
        "{g${A:-${B}}obby,other} sessions list",
        "unknown {app${A:-${B}}rove-dirty-paths,other}",
        r"$'\'/'{g,x}{o,y}bby sessions x",
        "$'\\'\"'/{g,x}{o,y}bby\"\"",
    ],
)
def test_symbolic_parameter_and_ansi_c_protected_words_are_refused(command: str) -> None:
    # Inert evaluator input only: never run these strings through a shell.
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "cd ${WT:-${HOME}}",
        "echo ${A:-${B}}",
        "echo ${A:-$(pwd)}",
        r"echo $'it\'s'",
        "echo '{gobby,}'",
        r"echo \{gobby,\}",
        'gobby tasks "{show,list}"',
        r"gobby \{tasks,x\} list",
    ],
)
def test_symbolic_parameter_and_ansi_c_data_controls_stay_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "g$()obby se$()ssions approve-dirty-pat$()hs s7 --checkout /repo a.txt",
        "g``obby se``ssions approve-dirty-pat``hs s7 --checkout /repo a.txt",
        'A=; g$A"obby" se$A"ssions" approve-dirty-pat$A"hs" s7 --checkout /repo a.txt',
        "g$()obby sessions list",
        "go$()bby sessions list",
        "unknown app$()rove-dirty-paths",
        "unknown app$(true)rove-dirty-paths",
        "g``obby sessions list",
        "go``bby sessions list",
        "unknown app``rove-dirty-paths",
        'g$A"obby" sessions list',
        'go$A"bby" sessions list',
        'unknown app$A"rove-dirty-paths"',
        "echo $(g$()obby sessions list)",
        "echo `g$()obby sessions list`",
    ],
)
def test_symbolic_substitution_and_variable_protected_words_are_refused(command: str) -> None:
    # Inert evaluator input only; substitutions and the approval CLI never run.
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "echo $(pwd)",
        "cd $HOME",
        'cat "${dir}/${file}"',
        "ls ${dir}/*",
        'ls "${HOME}/.gobby/bin/gobby"',
        "cat 'g$()obby'",
        r"cat g\$\(\)obby",
        'cat "$(pwd)/${file}"',
        'cat "`pwd`/${file}"',
    ],
)
def test_symbolic_substitution_and_variable_data_controls_stay_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize("command", ["echo $(echo {a,b})", "echo `echo {a,b}`"])
def test_symbolic_substitution_body_braces_stay_in_their_execution_context(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "g$@obby se$@ssions approve-dirty-pat$@hs s7 --checkout /repo a.txt",
        "g$1obby sessions list",
        "g$!obby sessions list",
        "unknown app$@rove-dirty-paths",
    ],
)
def test_special_parameter_protected_words_are_refused(command: str) -> None:
    # Evaluator data only: never expand these strings or execute the CLI.
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize("command", ['echo "$@"', "kill $!", "echo $1", "cd $HOME"])
def test_special_parameter_data_controls_stay_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "P=/josh/.local/bin/go; D=approve-dirty-paths; /Users${P}bby se$()ssions $D s7 --checkout /repo a.txt",
        "/Users${P}bby se$()ssions $D s7 --checkout /repo a.txt",
        "unknown /Users${P}bby sessions list",
        "unknown /Users$(true)bby sessions list",
        "unknown /Users`true`bby sessions list",
        "x$Ay sessions list",
    ],
)
def test_expansion_introduced_basename_is_refused(command: str) -> None:
    # Inert evaluator input: never resolve the paths or execute shell strings.
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command", ['cat "${dir}/${file}"', 'ls "${HOME}/.gobby/bin/gobby"', "ls /tmp/*by"]
)
def test_expansion_basename_data_and_literal_globs_stay_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ('gobby --config="$X" tasks list', False),
        ('gobby --config "path${X}" tasks list', False),
        ('unknown --config="$X" tasks list', True),
        ('unknown gobby --config="$X" tasks list', True),
        ('g${X}obby --config="$X" tasks list', True),
        ('gobb? --config="$X" tasks list', True),
        ('gobby --other="$X" tasks list', True),
        ('gobby --config="$X" tasks list; unknown --config="$X"', True),
        ('gobby --config="$X" tasks list | unknown --config="$X"', True),
        ('gobby tasks list; /Users${P}bby --config="$X" tasks list', True),
    ],
)
def test_config_data_exemption_requires_the_literal_gobby_command(
    command: str, refused: bool
) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])") is refused


@pytest.mark.parametrize(
    "command",
    [
        'echo "Hello $USER"',
        'export PATH="$HOME/bin:$PATH"',
        'curl -H "Authorization: Bearer $T"',
        "X=/tmp/$Y",
        'gobby --config="$X" tasks list',
        "declare X=/tmp/$Y",
        "local X=/tmp/$Y",
        "readonly X=/tmp/$Y",
        "typeset X=/tmp/$Y",
        'cd "prefix$X"',
    ],
)
def test_trailing_expansion_in_approved_data_contexts_stays_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "/Users${P} sessions list",
        "env /Users${P} sessions list",
        "x; /Users${P} sessions list",
        "P=/x/gobby /Users${P} sessions list",
        "unknowntool /Users${P} sessions list",
        'git commit -m "Bump to $V"',
        'gobby --other="$X" tasks list',
        "P=/josh/.local/bin/gobby; D=approve-dirty-paths; /Users${P} se$()ssions $D s7 --checkout /repo a.txt",
    ],
)
def test_trailing_expansion_in_executable_contexts_stays_refused(command: str) -> None:
    # Git operands keep the full transition: git aliases may execute operands.
    # Refusing the ordinary git commit message here is an accepted limitation.
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


def test_expansion_marker_in_brace_character_class_is_refused_without_error() -> None:
    assert approval_command_word_expansion("{${A}-z}obby sessions list")


def test_assignment_shaped_launcher_operand_keeps_full_basename_matching() -> None:
    # Inert data only; no symlink, expansion, or executable is created or run.
    command = "caffeinate X=${P} se$()ssions $D s7 --checkout /repo a.txt"
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


def test_path_named_like_a_declaration_builtin_is_an_unknown_head() -> None:
    command = "/tmp/export X=${P} se$()ssions $D"
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


def test_path_named_like_a_data_command_is_an_unknown_head() -> None:
    command = "/tmp/echo /Users${P} sessions list"
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        'echo() if true; then "$@"; fi; P=/josh/.local/bin/gobby; '
        "D=approve-dirty-paths; echo /Users${P} se$()ssions $D s7 --checkout /repo a.txt",
        'echo() if true; then "$@"; fi; echo /Users${P} sessions list',
        'echo() { "$@"; }; echo /Users${P} sessions list',
        'function echo { "$@"; }; echo /Users${P} sessions list',
        "alias echo=x\necho /Users${P} sessions list",
        "enable -n echo; echo /Users${P} sessions list",
        "hash -p /x/gobby cat; cat /Users${P} se$()ssions $D",
        ". /tmp/f; echo /Users${P} se$()ssions $D",
        "source /tmp/f; echo /Users${P} se$()ssions $D",
        'eval "$(cat /tmp/f)"; echo /Users${P} se$()ssions $D',
        "BASH_ENV=/tmp/f bash -c 'echo /Users${P} se$()ssions $D'",
        "ENV=/tmp/f; echo /Users${P} se$()ssions $D",
        "builtin enable -n echo; echo /Users${P} sessions list",
        "command alias echo=x; echo /Users${P} sessions list",
        "command builtin enable -n echo; echo /Users${P} sessions list",
        "echo /Users${P} sessions list; alias echo=x",
        "echo /Users${P} sessions list; bash -c 'alias echo=x'",
        "echo /Users${P} sessions list; bash -c 'echo() { true; }'",
        "bash -c 'echo /Users${P} sessions list'; alias echo=x",
        "echo /Users${P} sessions list; echo BASH_ENV=/tmp/f",
        "echo /Users${P} sessions list; echo ENV=/tmp/f",
        'alias x=y; export PATH="$HOME/bin:$PATH"',
        'alias x=y; gobby --config="$X" tasks list',
    ],
)
def test_shell_setup_disables_data_narrowing_for_the_whole_text(command: str) -> None:
    """Evaluate inert shell text only; never run setup or approval commands."""
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        'echo() if true; then "$@"; fi; D=approve-dirty-paths; '
        "echo gobby se$()ssions $D s7 --checkout /repo a.txt",
        "D=approve-dirty-paths; git -c alias.x='!gobby' x se$()ssions $D s7 --checkout /repo a.txt",
        "D=approve-dirty-paths; git $X gobby sessions $D s7 --checkout /repo a.txt",
    ],
)
def test_shadowed_data_and_git_heads_keep_expansion_slot_checks(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


def test_git_builtin_grep_operand_is_not_a_command_word() -> None:
    assert not approval_command_word_expansion("git log --grep gobby $X")


@pytest.mark.parametrize(
    "launcher",
    [
        "caffeinate",
        "arch -arm64",
        "/usr/bin/arch -arch arm64",
        "doas",
        "ionice -c3",
        "flock /tmp/approval.lock",
        "chronic",
        "pipx run",
        "poetry run",
        "rtk",
        "direnv exec .",
        "script -q /dev/null",
        "sandbox-exec -n profile",
        "taskset 1",
        "strace -f",
        "unknown",
    ],
)
@pytest.mark.parametrize("slot", ["$S", "{sessions,}"])
@pytest.mark.parametrize("context", ["{}", "cat x | {}", "cat <(echo hi) <({})"])
def test_unknown_launchers_keep_dynamic_gobby_slots_blocked(
    launcher: str, slot: str, context: str
) -> None:
    command = context.format(f"{launcher} gobby {slot} approve-dirty-paths s7")
    assert approval_command_word_expansion(command)


@pytest.mark.parametrize(
    "launcher",
    [
        "git bisect run",
        "git -C /r bisect run",
        "git submodule foreach",
        "git submodule foreach --recursive",
        "git x",
        "git -C /r x",
        "python3 -m uv run",
        "python3 -m pipx run",
        "uv run python -m pipx run",
    ],
)
@pytest.mark.parametrize("slot", ["$S", "{sessions,}"])
@pytest.mark.parametrize("context", ["{}", "cat x | {}", "cat <(echo hi) <({})"])
def test_git_and_python_module_launchers_keep_dynamic_gobby_slots_blocked(
    launcher: str, slot: str, context: str
) -> None:
    command = context.format(f"{launcher} gobby {slot} approve-dirty-paths s7")
    assert approval_command_word_expansion(command)


@pytest.mark.parametrize(
    "command",
    [
        "git log --grep gobby $X",
        "git show gobby $X",
        "git -C /repo/gobby log --grep gobby $X",
        'git add src/gobby "$F"',
        'git checkout gobby "$B"',
    ],
)
def test_git_builtin_operands_are_not_gobby_command_slots(command: str) -> None:
    assert not approval_command_word_expansion(command)


def test_python_script_argv_is_an_accepted_residual() -> None:
    # Orchestrator ruling: script/-c argv cannot bound arbitrary Python execution.
    assert not approval_command_word_expansion("python3 l.py gobby $S approve-dirty-paths s7")


@pytest.mark.parametrize(
    "command",
    ["echo gobby sessions list", "git grep gobby", 'git commit -m "gobby sessions list"'],
)
def test_literal_data_and_git_words_stay_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "git -calias.x='!gobby' x se$()ssions $D",
        "git -c Alias.X='!gobby' x se$()ssions $D",
        "G='!gobby' git --config-env=alias.x=G x se$()ssions $D",
        "G='!gobby' git --config-env alias.x=G x se$()ssions $D",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0='!gobby' "
        "git x se$()ssions $D",
        "GIT_CONFIG_PARAMETERS=ignored git x se$()ssions $D",
        "GIT_CONFIG_KEY_42=alias.x git x se$()ssions $D",
        "GIT_CONFIG_VALUE_42='!gobby' git x se$()ssions $D",
        "git -c alias.x='!gobby' x se{s,}sions list",
        'git -c alias.st=status st "$X"',
        "echo /Users${P} sessions list; git -c alias.st=status st",
    ],
)
def test_in_text_git_aliases_refuse_expansions_and_disable_data_narrowing(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.st=status st",
        "git -c alias.x='!gobby' x sessions list",
        'git -c user.name=x commit -m "$MSG"',
    ],
)
def test_literal_git_alias_and_non_alias_config_controls_stay_allowed(command: str) -> None:
    context = {"tool_input": {"command": command}}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert not evaluator.evaluate("approval_command_word_expansion(tool_input['command'])")
