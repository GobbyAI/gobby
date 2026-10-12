"""Segment-scoped command matching for block-effect command patterns (#21056).

``command_pattern`` sees one executable shell segment at a time. Heredoc bodies
are stdin data and leave the subject unless something can run them; the
consumer allow-list is ``cat``, ``tee``, ``git``, ``gh`` and a bare redirection,
and everything else fails closed.
"""

from __future__ import annotations

import shlex

import pytest

from gobby.hooks._ansi_c import ShellDialect
from gobby.hooks._normalization_shell import ShellScan, scan_shell_command
from gobby.workflows.engine import command_matching
from gobby.workflows.engine.command_matching import (
    command_patterns_match,
    executable_command_subjects,
    mask_quoted_spans,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "cat <(g\\\nem push x)",
        "cat >(g\\\nem push x)",
        "cat <\\\n(g\\\nem push x)",
        "cat >\\\n(g\\\nem push x)",
        "echo $\\\n(g\\\nem push x)",
        'echo "$\\\n(g\\\nem push x)"',
    ],
)
def test_split_substitution_command_matches(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize("delimiter", ["EOF", "'EOF'", '"EOF"'])
def test_heredoc_split_substitution_matches(delimiter: str, mask_quoted: bool) -> None:
    command = f"cat <<{delimiter}\n$\\\n(gem push x)\nEOF"
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted) is (
        delimiter == "EOF"
    )


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize("delimiter", ["EOF", "'EOF'", '"EOF"'])
def test_heredoc_split_output_process_sink_matches(delimiter: str, mask_quoted: bool) -> None:
    command = f"cat <<{delimiter} > >\\\n(bash)\ngem push x\nEOF"
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted) is True


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "(case x in x) gem push x;; esac)",
        "( case x in x) gem push x;; esac )",
        "(case x in (x) gem push x;; esac)",
        "(true; case x in x) gem push x;; esac)",
        "echo $(case y in y) gem push x;; esac)",
        "echo <(case y in (y) gem push x;; esac)",
        "echo `case y in y) gem push x;; esac`",
        "echo >(case y in (y) gem push x;; esac)",
        "(case x in x) case y in y) gem push x;; esac;; esac)",
        "case x in x) (case y in y) gem push x;; esac);; esac",
        "(case x in ')') gem push x;; esac)",
        "(case x in x) gem push x;& y) true;; esac)",
        "(case x in x) true;;& x) gem push x;; esac)",
        "(case x in\nx) gem push x\n;;\nesac)",
    ],
)
def test_grouped_case_commands_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
def test_nested_case_closes_only_its_own_frame(mask_quoted: bool) -> None:
    command = "(case x in x) case y in y) true;; esac;; z) gem push x;; esac)"
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "shopt -s extglob\ncase x in @(x|y)) gem push x;; esac",
        'case "x)" in("x)")gem push x;; esac',
        'case x in("x")gem push x;; esac',
    ],
)
def test_case_pattern_headers_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command,expected",
    [
        ("(echo case x in x) && gem push x", True),
        ("echo $(echo case x in y) ; gem push x", True),
        ("echo $(echo case) gem push x", False),
    ],
)
def test_case_words_in_arguments_keep_command_matching(
    command: str, expected: bool, mask_quoted: bool
) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted) is expected


@pytest.mark.parametrize("mask_quoted", [False, True])
def test_malformed_grouped_case_matching_does_not_raise(mask_quoted: bool) -> None:
    result = command_patterns_match(
        "(case x in x) gem push x)", pattern=GEM_PUSH, mask_quoted=mask_quoted
    )
    assert isinstance(result, bool)


@pytest.mark.parametrize("mask_quoted", [False, True])
def test_malformed_case_arm_tail_does_not_raise(mask_quoted: bool) -> None:
    assert command_patterns_match("case;<())(", pattern=r"case;", mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "body",
    ['true; "git" push', '"git" push; true', 'true\n"git" push', r"cd x && g\it push"],
)
def test_glued_case_arm_subshell_list_matches(body: str, mask_quoted: bool) -> None:
    pattern = r"^git\s+push\b"
    assert command_patterns_match(
        f"case x in a) ({body});; esac", pattern=pattern, mask_quoted=mask_quoted
    )
    assert command_patterns_match(
        f"case x in a)({body});; esac", pattern=pattern, mask_quoted=mask_quoted
    )


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command,pattern",
    [
        ("case x in a)(touch a.py);; esac", r"^touch\b"),
        ("case x in a) (touch a.py);; esac", r"^touch\b"),
        ("case x in a)(rm -rf src);; esac", r"^rm\b"),
        ("case x in a) (rm -rf src);; esac", r"^rm\b"),
        ("case x in (a)(touch a.py);; esac", r"^touch\b"),
        ("case x in (a) (touch a.py);; esac", r"^touch\b"),
        ("case x in a)(touch a.py)&&(touch b.py);; esac", r"^touch a\.py\b"),
        ("case x in a) (touch a.py)&&(touch b.py);; esac", r"^touch a\.py\b"),
        ('case x in a)("git" push);; esac', r"^git\s+push\b"),
        ('case x in a) ("git" push);; esac', r"^git\s+push\b"),
        ("case x in a)(g\\it push);; esac", r"^git\s+push\b"),
        ("case x in a) (g\\it push);; esac", r"^git\s+push\b"),
        ("case x in ${x:-(})touch a.py;; esac", r"^touch\b"),
        ("case x in ${x:-)})touch a.py;; esac", r"^touch\b"),
    ],
)
def test_case_arm_boundary_preserves_command_matching(
    command: str, pattern: str, mask_quoted: bool
) -> None:
    assert command_patterns_match(command, pattern=pattern, mask_quoted=mask_quoted) is True


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "echo $(true)$(case y in y) touch x;; esac)",
        "echo $(true)$(true)$(case y in y) touch x;; esac)",
        "echo `true`$(case y in y) touch x;; esac)",
        "echo $(true)`case y in y) touch x;; esac`",
    ],
)
def test_adjacent_case_substitutions_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(
        command, pattern=r"(^|(?<=[;&|(`\n]))\s*touch\b", mask_quoted=mask_quoted
    )


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "echo '$(true)$(case y in y) touch x;; esac)'",
        "echo $(true)$(echo 'case y in y) touch x;; esac')",
    ],
)
def test_adjacent_case_substitution_literals_stay_data(command: str, mask_quoted: bool) -> None:
    assert not command_patterns_match(
        command, pattern=r"(^|(?<=[;&|(`\n]))\s*touch\b", mask_quoted=mask_quoted
    )


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        '(case x in x) touch x; echo "unterminated;; esac)',
        "echo $(case x in x) touch x; echo 'unterminated;; esac)",
        "(case x in x) touch x;; esac)\\",
    ],
)
def test_malformed_case_quotes_keep_fail_closed_matching(command: str, mask_quoted: bool) -> None:
    assert executable_command_subjects(command) == [command]
    assert command_patterns_match(command, pattern=r"\btouch\b", mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param("{ case x in x) gem push x;; esac; }", True, id="R1"),
        pytest.param("if true; then case x in x) gem push x;; esac; fi", True, id="R2"),
        pytest.param("! case x in x) gem push x;; esac", True, id="R3"),
        pytest.param("time case x in x) gem push x;; esac", True, id="R4"),
        pytest.param("time -p case x in x) gem push x;; esac", True, id="R5"),
        pytest.param("while true; do case x in x) gem push x;; esac; done", True, id="R6"),
        pytest.param("if case x in x) gem push x;; esac; then :; fi", True, id="R7"),
        pytest.param("while case x in x) gem push x;; esac; do :; done", True, id="R8"),
        pytest.param("until case x in x) gem push x;; esac; do :; done", True, id="R9"),
        pytest.param("if false; then :; else case x in x) gem push x;; esac; fi", True, id="R10"),
        pytest.param(
            "if false; then :; elif case x in x) gem push x;; esac; then :; fi", True, id="R11"
        ),
        pytest.param("if ! case x in x) gem push x;; esac; then :; fi", True, id="R12"),
        pytest.param("{ time case x in x) gem push x;; esac; }", True, id="R13"),
        pytest.param('eval "case x in x) gem push x;; esac"', True, id="R14"),
        pytest.param('bash -c "time case x in x) gem push x;; esac"', True, id="R15"),
        pytest.param("true && { case x in x) gem push x;; esac; }", True, id="R16"),
        pytest.param("{ case x in x)gem push x;; esac; }", True, id="R17"),
        pytest.param("if true; then case x in x)gem push x;; esac; fi", True, id="R18"),
        pytest.param("time case x in 'x)' | y) gem push x;; esac", True, id="R19"),
        pytest.param("if true; then case x in x) echo ok;; esac; fi", False, id="R20"),
        pytest.param("time case x in x) echo gem push x;; esac", False, id="R21"),
    ],
)
def test_case_header_reserved_word_prefixes(
    command: str, expected: bool, mask_quoted: bool
) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted) is expected


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "case x in(x)gem push x;; esac",
        "case x in(x) gem push x;; esac",
        "case x in(x|y) gem push x;; esac",
    ],
)
def test_case_header_unspaced_keyword_matches_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        pytest.param("case x in x)gem push x;; esac", id="G1"),
        pytest.param("case x in (x)gem push x;; esac", id="G3"),
        pytest.param("case x in x|y)gem push x;; esac", id="G4"),
        pytest.param("case x in *)gem push x;; esac", id="G5"),
        pytest.param("case x in x) echo ok;; y)gem push x;; esac", id="G6"),
        pytest.param("case x in x)gem push x;;esac", id="G7"),
        pytest.param('case x in "x")gem push x;; esac', id="G8"),
        pytest.param("case x in x) echo ok;& y)gem push x;; esac", id="G9"),
        pytest.param("case x in\nx)gem push x\n;;\nesac", id="G10"),
        pytest.param("case x in x) echo ok;;& y)gem push x;; esac", id="G12"),
        pytest.param("case x in 'x)' | y) gem push x;; esac", id="Q1"),
        pytest.param("case x in $(echo x) | y) gem push x;; esac", id="Q2"),
        pytest.param('case x in "x)" ) gem push x;; esac', id="Q3"),
        pytest.param("case x in $(echo x) ) gem push x;; esac", id="Q4"),
        pytest.param("case x in $'x)' | y) gem push x;; esac", id="Q5"),
        pytest.param(r"case x in \) | y) gem push x;; esac", id="Q6"),
        pytest.param('case x in "$(echo x)" | y) gem push x;; esac', id="Q7"),
        pytest.param("case 1 in $((1)) | 2) gem push x;; esac", id="Q8"),
        pytest.param("case x in y | 'x)' | z) gem push x;; esac", id="Q10"),
    ],
)
def test_case_pattern_terminator_boundary_matches_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        pytest.param("case x in x) gem push x;; esac", id="C1"),
        pytest.param('case x in "x)") gem push x;; esac', id="C2"),
        pytest.param(r"case x in x\)) gem push x;; esac", id="C3"),
        pytest.param("case x in $(echo x)) gem push x;; esac", id="C4"),
        pytest.param('case x in "$(echo x)") gem push x;; esac', id="C5"),
        pytest.param('case x in x) echo "a)"; gem push x;; esac', id="C6"),
        pytest.param("case x in x) echo $(echo a); gem push x;; esac", id="C7"),
        pytest.param('case x in x | "y)") gem push x;; esac', id="C8"),
    ],
)
def test_case_pattern_terminator_controls_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
def test_case_pattern_terminator_quoted_prose_does_not_match_blocks(mask_quoted: bool) -> None:
    assert not command_patterns_match(
        'echo "case x in x)gem push x;; esac"', pattern=GEM_PUSH, mask_quoted=mask_quoted
    )


GEM_PUSH = r"(^|(?<=[;&|(`\n]))\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*gem\s+push\b"


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "f() (case x in x) gem push x;; esac); f",
        "f() { gem push x; }; f",
        "function f (case x in x) gem push x;; esac); f",
        "f() (gem push x); f",
        "f () (case x in x) gem push x;; esac); f",
        "function f() { gem push x; }; f",
        "if true; then f() (case x in x) gem push x;; esac); f; fi",
        "f() { g() (case x in x) gem push x;; esac); g; }; f",
        "f(){ gem push x; }; f",
        "f()(case x in x) gem push x;; esac); f",
        "if f() { gem push x; }; f; then :; fi",
        "while f() { gem push x; }; f; do break; done",
        "case x in x) f() { gem push x; }; f;; esac",
        "! f() { gem push x; }; f",
        "f() while gem push x; do break; done; f",
        "f() if gem push x; then :; fi; f",
        "f() case x in x) gem push x;; esac; f",
        "f() { gem push x; echo {; }; f",
        "my-func() { gem push x; }; my-func",
        "a.b() { gem push x; }; a.b",
        "a:b() { gem push x; }; a:b",
        "time f() { gem push x; }; f",
        "time -p f() { gem push x; }; f",
    ],
)
def test_invoked_function_body_matches_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' 'f() { gem push x; }; f'",
        "f() { printf '%s' 'gem push x'; }; f",
        "cat <<'EOF'\nf() { gem push x; }; f\nEOF",
        'echo "f() { gem push x; }"',
    ],
)
def test_function_body_literal_does_not_match_blocks(command: str, mask_quoted: bool) -> None:
    assert not command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.timeout(5)
@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize("definition_count", [40, 300])
def test_many_function_definitions_match_blocks(mask_quoted: bool, definition_count: int) -> None:
    definitions = [f"f{i}() {{ echo {i}; }}" for i in range(definition_count - 1)]
    last = f"f{definition_count - 1}"
    command = "; ".join([*definitions, f"{last}() {{ gem push x; }}", last])
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "case b in a) f() { :; };; b) g() { gem push x; };; esac; g",
        "case b in (a) f() { :; };; (b) g() { gem push x; };; esac; g",
        "case b in a) function f { :; };; b) function g { gem push x; };; esac; g",
    ],
)
def test_later_case_arm_function_matches_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.timeout(5)
@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize("wrapper", ["echo $({body})", "cat <({body})", "( {body} )"])
def test_nested_wrappers_with_many_functions_match_blocks(mask_quoted: bool, wrapper: str) -> None:
    command = "; ".join([*(f"p{j}() {{ echo {j}; }}" for j in range(300)), "gem push x"])
    for i in range(8):
        command = f"f{i}() {{ :; }}; " + wrapper.format(body=command)
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize("levels", [8, 9, 10])
def test_deep_substitution_function_matches_blocks(mask_quoted: bool, levels: int) -> None:
    command = "f() { gem push x; }; f"
    for _ in range(levels):
        command = f"echo $( {command} )"
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "f() \\\n{ gem push x; }; f",
        "function f \\\n{ gem push x; }; f",
        "f\\\n() { gem push x; }; f",
        "f(\\\n) { gem push x; }; f",
        "f (\\\n) { gem push x; }; f",
        "function \\\nf { gem push x; }; f",
        "function f\\\n() { gem push x; }; f",
        "f\\\noo() { gem push x; }; foo",
        "# '\nf\\\n() { gem push x; }; f",
        "echo $'x\\'y'; f\\\n() { gem push x; }; f",
        "cat <<'EOF'\n'\nEOF\nf\\\n() { gem push x; }; f",
        "bash <<'EOF'\nf\\\n() { gem push x; }; f\nEOF",
        "bash <<'EOF'\nf() { gem push x; }; f\nEOF",
    ],
)
def test_function_continuation_and_heredoc_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("gem push x <<-#\\\nx", True),
        ("gem push x; bash -c 'true <<-#\\\nx'", True),
        ("x=$(true <<-#\\\nx); gem push x", True),
        ("\\<<-#; f() { gem push x; }; f", True),
        ("true <<-#\\\nx", False),
        ("true <<-\\\n#\\\nx", False),
        ("true <<<-#\\\nx", False),
        ("true <<<<<-#\\\nx", False),
        ("true <<<<<<<<-#\\\nx", False),
        ("true <\\\n<<-#\\\nx", False),
        ("true <<<\\\n<<-#\\\nx", False),
        ("true <<\\\n-#\\\nx", False),
        ("true <\\\n<-#\\\nx", False),
        ("gem push x <<<<<-#\\\nx", True),
    ],
)
def test_heredoc_comment_continuation_matches(
    command: str, expected: bool, mask_quoted: bool
) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted) is expected


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "cat <\\\n<<-# ; gem push x",
        "cat <<\\\n-EOF\nEOF\ngem push x\n-EOF",
        "cat <<\\\n< x\ngem push x\nx",
        "bash -c 'cat <\\\n<<-# ; gem push x'",
    ],
)
def test_split_operator_continuation_matches(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
def test_quoted_function_header_continuation_stays_literal(mask_quoted: bool) -> None:
    command = "echo 'f\\\n() { gem push x; }'"
    assert not command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
def test_nine_nested_function_definitions_match_blocks(mask_quoted: bool) -> None:
    command = "gem push x"
    for index in range(9):
        command = f"f{index}() {{ {command}; }}; f{index}"
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param('gem push x ; echo "\\', True, id="simple-match"),
        pytest.param('gem push x\necho "\\', True, id="newline-match"),
        pytest.param("sh <<'EOF'\ngem push x\necho \"\\\nEOF", True, id="sh-heredoc-match"),
        pytest.param("bash <<'EOF'\ngem push x\necho \"\\\nEOF", True, id="bash-heredoc-match"),
        pytest.param("bash -c 'gem push x\necho \"\\'", True, id="bash-c-match"),
        pytest.param('echo "literal\\', False, id="simple-control"),
        pytest.param(
            "gem push x ; cat <<'EOF'\n\"\nEOF\necho \\",
            True,
            id="quoted-heredoc-match",
        ),
        pytest.param(
            "cat <<'EOF'\n\"\nEOF\necho \\",
            False,
            id="quoted-heredoc-control",
        ),
    ],
)
def test_unterminated_double_quote_trailing_backslash(
    command: str, expected: bool, mask_quoted: bool
) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted) is expected


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "echo $$'\\' ; gem push x ; echo '\\'",
        "echo $(echo $$'\\') ; gem push x ; echo ')'\\'",
        "echo $$'\\'' ; gem push x ; echo \\'",
    ],
)
def test_dollar_pair_quote_commands_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize("command", ["echo $$'.txt'", "echo 'a ; gem push x'"])
def test_dollar_pair_quote_literal_does_not_match_blocks(command: str, mask_quoted: bool) -> None:
    assert not command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "case x in x) gem push x;; esac",
        "case x in (x) gem push x;; esac",
        "case x in y) true;; x) gem push x;; esac",
        "case x in y|x) gem push x;; esac",
        "case x in\nx) gem push x;;\nesac",
        "case x in x) case y in y) gem push x;; esac;; esac",
    ],
)
def test_case_arm_commands_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "case x in 'gem push x') true;; esac",
        'case x in "gem push x") true;; esac',
        "echo 'x) gem push x'",
        "case x in x) echo hi;; esac",
    ],
)
def test_case_pattern_literals_do_not_match_blocks(command: str, mask_quoted: bool) -> None:
    assert not command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "if true; then gem push x; fi",
        "while true; do gem push x; done",
        "{ gem push x; }",
        "true && gem push x",
    ],
)
def test_compound_commands_match_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)


PROSE_BODY = "Run `git commit` in the shared checkout, never `git push --force`."
COMMIT_PATTERN = r"(^|(?<=[;&|(`\n]))\s*git\s+commit\b"
PYTEST_PATTERN = r"(^|(?<=[;&|(`\n]))\s*(?:uv\s+run\s+)?pytest\b"
DAEMON_PATTERN = (
    r"(^|(?<=[;&|(`\n]))\s*(?:[^\s;&|]*/)?(?:uv\s+run\s+)?gobby\s+(start|stop|restart)\b"
)
GUARD_EXEMPTION = r"(?s)(?=.*GOBBY_TEST_PROTECT=1)(?=.*DATABASE_URL=)"


class TestExecutableCommandSubjects:
    def test_segments_are_pipelines_with_raw_quotes_and_substitutions(self) -> None:
        command = 'cd /repo && echo "a; b" |\n  wc -l\nls $(pwd) || true'

        assert executable_command_subjects(command) == [
            "cd /repo",
            'echo "a; b" |\n  wc -l',
            "ls $()",
            "pwd",
            "true",
            "ls $(pwd)",
        ]

    def test_quoted_substitution_redirect_keeps_the_whole_segment(self) -> None:
        command = 'echo "$(printf x > out.txt)" && git status'

        assert executable_command_subjects(command) == [
            'echo "$()"',
            "printf x > out.txt",
            "git status",
            'echo "$(printf x > out.txt)"',
        ]

    @pytest.mark.parametrize(
        "opener",
        [
            "cat >> notes.md",
            "tee -a notes.md",
            "> notes.md",
            "git commit -F -",
            "gh pr create --body-file -",
            "GIT_EDITOR=true cat",
        ],
    )
    def test_quoted_heredoc_to_a_data_consumer_is_dropped(self, opener: str) -> None:
        command = f"{opener} <<'EOF'\n{PROSE_BODY}\nEOF\necho done"

        assert executable_command_subjects(command) == [f"{opener} <<'EOF'", "echo done"]

    @pytest.mark.parametrize(
        "opener",
        [
            "bash",
            "sh",
            "python -",
            "zsh -s",
            "ssh build-host",
            "unknown-tool",
            "sudo cat",
            "xargs -0",
        ],
    )
    def test_heredoc_to_an_interpreter_or_unknown_consumer_has_its_own_subject(
        self, opener: str
    ) -> None:
        command = f"{opener} <<'EOF'\n{PROSE_BODY}\nEOF"

        assert executable_command_subjects(command) == [f"{opener} <<'EOF'", PROSE_BODY]

    def test_body_piped_onward_has_its_own_subject_when_a_stage_can_run_it(self) -> None:
        assert executable_command_subjects("cat <<'EOF' | bash\nbody\nEOF") == [
            "cat <<'EOF' | bash",
            "body",
        ]
        assert executable_command_subjects("cat <<'EOF' | tee out.txt\nbody\nEOF") == [
            "cat <<'EOF' | tee out.txt"
        ]
        # A data stage — sed only transforms the bytes — keeps the body data.
        assert executable_command_subjects("cat <<'EOF' | sed s/x/y/ > docs/ops.md\nbody\nEOF") == [
            "cat <<'EOF' | sed s/x/y/ > docs/ops.md"
        ]
        # The body precedes the next stage even when the pipeline continues.
        assert executable_command_subjects("cat <<'EOF' |\nbody\nEOF\n  bash") == [
            "cat <<'EOF' |\n  bash",
            "body",
        ]

    def test_data_consumer_heredoc_stays_data_except_into_interpreters(self) -> None:
        body = "uv run gobby restart"

        piped_through_sed = f"cat <<'EOF' | sed 's/x/y/' > docs/ops.md\n{body}\nEOF"
        assert executable_command_subjects(piped_through_sed) == [
            "cat <<'EOF' | sed 's/x/y/' > docs/ops.md"
        ]
        assert not command_patterns_match(piped_through_sed, pattern=DAEMON_PATTERN)
        wrapped = ("sudo bash", "env -i bash", "xargs -0 sh -c")
        for interpreter in ("bash", "sh", "zsh", "eval", *wrapped):
            into_shell = f"cat <<'EOF' | {interpreter}\n{body}\nEOF"
            assert command_patterns_match(into_shell, pattern=DAEMON_PATTERN), interpreter

    def test_output_process_substitution_keeps_the_body(self) -> None:
        command = "cat <<'EOF' > >(bash)\nbody\nEOF"

        assert executable_command_subjects(command) == ["cat <<'EOF' > >(bash)", "bash", "body"]

    def test_unquoted_delimiter_contributes_only_substitution_spans(self) -> None:
        assert executable_command_subjects("cat <<EOF\n$(git push --force)\nEOF") == [
            "cat <<EOF",
            "git push --force",
        ]
        assert executable_command_subjects("cat <<EOF\n`git push --force`\nEOF") == [
            "cat <<EOF",
            "git push --force",
        ]
        assert executable_command_subjects("cat <<EOF\nplain $HOME prose\nEOF") == ["cat <<EOF"]

    def test_unquoted_body_text_beside_a_substitution_stays_out(self) -> None:
        command = 'cat > notes.md <<EOF\n`git push --force`\npytest.importorskip("yaml")\nEOF'

        assert executable_command_subjects(command) == ["cat > notes.md <<EOF", "git push --force"]
        assert not command_patterns_match(command, pattern=PYTEST_PATTERN)
        # A substitution span carrying a real invocation still selects a block.
        assert command_patterns_match("cat <<EOF\n`uv run pytest`\nEOF", pattern=PYTEST_PATTERN)

    def test_substitution_heredoc_to_a_data_consumer_is_dropped(self) -> None:
        """A commit message built from `cat <<'EOF'` is data one level in."""
        command = "git commit -m \"$(cat <<'EOF'\nfix: guard\n\npytest now passes\nEOF\n)\""

        assert executable_command_subjects(command) == [
            'git commit -m "$()"',
            "cat <<'EOF'",
            "git commit -m \"$(cat <<'EOF')\"",
        ]
        assert not command_patterns_match(command, pattern=PYTEST_PATTERN, mask_quoted=True)

    def test_substitution_that_runs_a_command_keeps_it(self) -> None:
        command = 'git commit -m "$(uv run pytest)"'

        assert executable_command_subjects(command) == [
            'git commit -m "$()"',
            "uv run pytest",
            command,
        ]
        assert command_patterns_match(command, pattern=PYTEST_PATTERN, mask_quoted=True)

    def test_a_shell_segment_keeps_its_substitution_body(self) -> None:
        """``sh -c "$(cat ...)"`` executes what the substitution prints."""
        for runner in ("bash -c", "sh -c", "eval"):
            command = f"{runner} \"$(cat <<'EOF'\npytest\nEOF\n)\""

            assert executable_command_subjects(command) == [command]
            assert command_patterns_match(command, pattern=PYTEST_PATTERN, mask_quoted=True)

    def test_single_quoted_substitution_is_literal_text(self) -> None:
        command = "echo '$(uv run pytest)'"

        assert executable_command_subjects(command) == [command]
        assert not command_patterns_match(command, pattern=PYTEST_PATTERN, mask_quoted=True)

    def test_nested_substitution_correlations_do_not_duplicate_correlations(self) -> None:
        command = "echo x"
        for _ in range(18):
            command = f"echo $({command})"

        subjects = executable_command_subjects(command)

        assert "echo x" in subjects
        assert len(subjects) == 37
        assert sum(map(len, subjects)) <= len(command) * len(subjects)

    def test_unterminated_heredoc_keeps_its_swallowed_text(self) -> None:
        command = "cat <<'EOF'\ngit push --force"

        assert executable_command_subjects(command) == ["cat <<'EOF'", "git push --force"]

    def test_unparseable_or_empty_commands_are_matched_whole(self) -> None:
        assert executable_command_subjects("echo 'open") == ["echo 'open"]
        assert executable_command_subjects("   ") == ["   "]


class TestCommandPatternsMatch:
    def test_missing_pattern_is_unconstrained(self) -> None:
        assert command_patterns_match("anything", pattern=None)
        assert command_patterns_match("anything", pattern="")

    def test_line_anchored_pattern_sees_each_segment_as_its_own_line(self) -> None:
        assert command_patterns_match("cd /repo && uv run pytest", pattern=r"^uv run pytest\s*$")
        assert not command_patterns_match(
            "cd /repo && uv run pytest tests/x.py", pattern=r"^uv run pytest\s*$"
        )

    def test_exemption_reads_the_whole_executable_text(self) -> None:
        exported = (
            "export GOBBY_TEST_PROTECT=1\n"
            "export DATABASE_URL=postgresql://gobby_test@127.0.0.1:60892/gobby_test\n"
            "uv run pytest tests/x.py"
        )

        assert not command_patterns_match(
            exported, pattern=PYTEST_PATTERN, not_pattern=GUARD_EXEMPTION
        )
        assert command_patterns_match(
            "cd /repo && uv run pytest tests/x.py",
            pattern=PYTEST_PATTERN,
            not_pattern=GUARD_EXEMPTION,
        )

    def test_inert_heredoc_prose_cannot_select_a_block(self) -> None:
        assert not command_patterns_match(
            f"cat >> notes.md <<'EOF'\n{PROSE_BODY}\nEOF", pattern=COMMIT_PATTERN
        )
        assert command_patterns_match(f"bash <<'EOF'\n{PROSE_BODY}\nEOF", pattern=COMMIT_PATTERN)

    def test_mask_quoted_blanks_string_data_inside_each_subject(self) -> None:
        command = "bash <<'EOF'\necho 'x\ngit commit'\nEOF"

        assert command_patterns_match(command, pattern=COMMIT_PATTERN)
        assert not command_patterns_match(command, pattern=COMMIT_PATTERN, mask_quoted=True)

    def test_quoted_separator_is_not_command_boundary(self) -> None:
        assert not command_patterns_match("gcode grep -E 'pytest|vitest'", pattern=PYTEST_PATTERN)
        assert not command_patterns_match('gcode grep -E "pytest|vitest"', pattern=PYTEST_PATTERN)
        assert not command_patterns_match("echo 'x; pytest'", pattern=PYTEST_PATTERN)
        assert not command_patterns_match("echo 'x & pytest'", pattern=PYTEST_PATTERN)
        assert not command_patterns_match("echo '(pytest)'", pattern=PYTEST_PATTERN)
        assert not command_patterns_match("echo '`pytest`'", pattern=PYTEST_PATTERN)

    def test_unquoted_separators_still_start_commands(self) -> None:
        assert command_patterns_match("echo x | pytest", pattern=PYTEST_PATTERN)
        assert command_patterns_match("x; pytest", pattern=PYTEST_PATTERN)
        assert command_patterns_match("x & pytest", pattern=PYTEST_PATTERN)
        assert command_patterns_match("echo $(pytest)", pattern=PYTEST_PATTERN)
        assert command_patterns_match("echo `pytest`", pattern=PYTEST_PATTERN)
        assert command_patterns_match('echo "$(pytest)"', pattern=PYTEST_PATTERN)

    def test_exemption_still_sees_quoted_test_path(self) -> None:
        not_pattern = (
            r"\bpytest\b[^\n;&|]*\s(?:-k|[^\s;&|-]\S*\.py\b|"
            r"['\"]?(?:[^\s;&|'\"-][^\s;&|]*/)?tests/[^\s/;&|'\"]+)"
        )

        assert not command_patterns_match(
            "pytest 'tests/x/test_y.py'",
            pattern=PYTEST_PATTERN,
            not_pattern=not_pattern,
        )
        assert command_patterns_match(
            "pytest",
            pattern=PYTEST_PATTERN,
            not_pattern=not_pattern,
        )

    def test_masked_matching_still_exempts_over_the_unmasked_text(self) -> None:
        """Masking hides prose from the pattern, never a path from the exemption."""
        not_pattern = (
            r"\bpytest\b[^\n;&|]*\s(?:-k|[^\s;&|-]\S*\.py\b|"
            r"['\"]?(?:[^\s;&|'\"-][^\s;&|]*/)?tests/[^\s/;&|'\"]+)"
        )

        assert not command_patterns_match(
            "pytest 'tests/x/test_y.py'",
            pattern=PYTEST_PATTERN,
            not_pattern=not_pattern,
            mask_quoted=True,
        )
        assert command_patterns_match(
            "pytest",
            pattern=PYTEST_PATTERN,
            not_pattern=not_pattern,
            mask_quoted=True,
        )


def test_mask_quoted_is_comment_aware() -> None:
    """A `#` comment ends at the newline, so a following line stays code.

    The masker reads quotes before it knows about comments, so an apostrophe
    inside a comment used to open an unterminated single-quoted span that
    blanked the real invocation on the next line (#23134).
    """
    command = "# don't\ngit commit"
    assert "git commit" in mask_quoted_spans(command)
    assert command_patterns_match(command, pattern=COMMIT_PATTERN, mask_quoted=True)

    assert command_patterns_match(
        "echo hi # it's\ngit commit", pattern=COMMIT_PATTERN, mask_quoted=True
    )
    # A real trailing comment is still a comment, and a real invocation
    # carrying one still blocks.
    assert command_patterns_match(
        "git commit -m 'x' # ok", pattern=COMMIT_PATTERN, mask_quoted=True
    )


def test_mask_quoted_unwraps_literal_execution_wrappers() -> None:
    """A quoted script that a shell/xargs/eval/timeout/watch/ssh runs stays code."""
    for command in (
        "bash -c 'git commit'",
        'bash -c "git commit"',
        "bash -euo pipefail -c 'git commit'",
        "bash -eo pipefail -c 'git commit'",
        "bash -euO extglob -c 'git commit'",
        "eval 'git commit'",
        "echo 1 | xargs -I{} git commit",
        "timeout 5 git commit",
        "watch -n1 'git commit'",
        "ssh host 'git commit'",
    ):
        assert command_patterns_match(command, pattern=COMMIT_PATTERN, mask_quoted=True), command

    # The same words inside a data string or a data heredoc stay data.
    assert not command_patterns_match('echo "git commit"', pattern=COMMIT_PATTERN, mask_quoted=True)
    assert not command_patterns_match(
        "cat <<'EOF'\ngit commit\nEOF", pattern=COMMIT_PATTERN, mask_quoted=True
    )


def test_selectors_reuse_one_commands_subject_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every rule's selector reads the same command; it is scanned once (#23359)."""
    scanned: list[str] = []

    def counting_scan(command: str, *, dialect: ShellDialect = "bash") -> ShellScan:
        scanned.append(command)
        return scan_shell_command(command, dialect=dialect)

    monkeypatch.setattr("gobby.workflows.engine.command_matching.scan_shell_command", counting_scan)
    command = "git status && bash -c 'git push origin main' # selector reuse (#23359)"

    assert command_patterns_match(command, pattern=r"\bgit\s+push\b")
    first_scans = len(scanned)
    assert first_scans > 0
    assert not command_patterns_match(command, pattern=COMMIT_PATTERN)
    assert command_patterns_match(command, pattern=r"\bgit\s+status\b")

    assert len(scanned) == first_scans


def _nested_bash(command: str, levels: int) -> str:
    for _ in range(levels):
        command = f"bash -c {shlex.quote(command)}"
    return command


@pytest.mark.parametrize(
    ("command", "outcomes"),
    [
        ("git commit -m 'feat: x' && git push", {True, False}),
        ('echo "git commit"', {True, False}),
        ("cat <<'EOF'\ngit commit\nEOF", {True, False}),
        ("uv run pytest tests/x.py", {True, False}),
        ("ssh host 'git commit' # it's", {True, False}),
        # Nested past the wrapper bound: every selector fails closed.
        (_nested_bash("git status", command_matching._WRAPPER_DEPTH + 2), {True}),
    ],
)
def test_selector_cache_hits_match_misses(command: str, outcomes: set[bool]) -> None:
    """A cached subject derivation decides every selector as a fresh one does."""
    selectors = [
        (pattern, not_pattern, mask_quoted, resolve_uv_run)
        for pattern in (COMMIT_PATTERN, PYTEST_PATTERN, r"^(?:echo|cat)\b", r"\bnever\b")
        for not_pattern in (None, r"--no-verify|\bpush\b")
        for mask_quoted in (False, True)
        for resolve_uv_run in (False, True)
    ]

    def decide(selector: tuple[str, str | None, bool, bool]) -> bool:
        pattern, not_pattern, mask_quoted, resolve_uv_run = selector
        return command_patterns_match(
            command,
            pattern=pattern,
            not_pattern=not_pattern,
            mask_quoted=mask_quoted,
            resolve_uv_run=resolve_uv_run,
        )

    misses = []
    for selector in selectors:
        command_matching._selector_subjects.cache_clear()
        misses.append(decide(selector))
    hits = [decide(selector) for selector in selectors]

    assert hits == misses
    assert set(misses) == outcomes


@pytest.mark.parametrize("mask_quoted", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "echo hi | (case x in x) gem push x;; esac)",
        "! (case x in x) gem push x;; esac)",
        "time (case x in x) gem push x;; esac)",
        "if (case x in x) gem push x;; esac); then :; fi",
        "{ (case x in x) gem push x;; esac); }",
        "while (case x in x) gem push x;; esac); do :; done",
        "echo hi |& (case x in x) gem push x;; esac)",
        "time -p (case x in x) gem push x;; esac)",
        "until (case x in x) gem push x;; esac); do :; done",
        "if true; then (case x in x) gem push x;; esac); fi",
        "if true; then :; else (case x in x) gem push x;; esac); fi",
        "if false; then :; elif (case x in x) gem push x;; esac); then :; fi",
        "while true; do (case x in x) gem push x;; esac); done",
        "echo hi | case x in x) gem push x;; esac",
    ],
)
def test_command_position_group_case_matches_blocks(command: str, mask_quoted: bool) -> None:
    assert command_patterns_match(command, pattern=GEM_PUSH, mask_quoted=mask_quoted)
