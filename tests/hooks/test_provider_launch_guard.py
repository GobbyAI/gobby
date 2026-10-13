"""Literal launch classification and real-engine enforcement without launching providers."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks import provider_launch_guard
from gobby.hooks._provider_shell_syntax import _prepare
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.provider_launch_guard import blocks_direct_provider_launch
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_rule_file

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        r"echo `echo \`claude\``",
        r'echo "`echo \`claude\``"',
        r"echo `echo \`echo \\\`claude\\\`\``",
        r"echo `echo \`claude`",
        "echo $(echo $(claude))",
    ],
)
def test_nested_escaped_backticks_block_provider_launch(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [r"echo '`echo \`claude\``'", r"echo `echo '\`claude\`'`"],
)
def test_nested_escaped_backticks_literal_data(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is False


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        (
            "f() { g; }; g() { bash; }; h() { f() { g; }; g() { cat; }; echo hi | f; }; "
            "h | python3; echo 'claude -p hi' | f",
            True,
        ),
        (
            "f() { g; }; g() { python3; }; h() { f() { g; }; g() { cat; }; echo hi | f; }; "
            "h | bash; echo 'import os; os.system(\"claude -p hi\")' | f",
            True,
        ),
        (
            "f() { g; }; g() { bash; }; "
            "bash -c 'f() { g; }; g() { cat; }; echo hi | f'; echo 'claude -p hi' | f",
            True,
        ),
        (
            "f() { g; }; g() { cat; }; "
            "bash -c 'f() { g; }; g() { bash; }; echo hi | f'; echo 'claude -p hi' | f",
            False,
        ),
    ],
)
def test_function_consumer_memo_isolated_by_evaluation_scope(command: str, blocked: bool) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is blocked


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("f() { cat; }; echo hi | f; f() { bash; }; echo 'claude -p hi' | f", True),
        ("f() { cat; }; f <<< hi; f() { bash; }; f <<< 'claude -p hi'", True),
        ("f() { g; }; g() { cat; }; echo hi | f; g() { bash; }; echo 'claude -p hi' | f", True),
        ("a() { bash; }; b() { bash; }; echo hi | a; echo 'claude -p hi' | b", True),
        ("f() { g() { bash; }; echo 'claude -p hi' | g; }; g() { cat; }; echo hi | g; f", True),
        ("f() { bash; }; echo hi | f; f() { cat; }; echo 'claude -p hi' | f", False),
        ("f() { g; }; g() { bash; }; echo hi | f; g() { cat; }; echo 'claude -p hi' | f", False),
        ("f() { f; f; f; }; echo hi | f", False),
        ("f() { bash; }; echo 'claude -p hi' | f; f() { cat; }; echo hi | f", True),
        ("f() { bash; }; echo hi | f; f() { cat; }; echo hi | f", False),
    ],
)
def test_function_consumer_memo_tracks_redefinitions(command: str, blocked: bool) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is blocked


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("echo x | sed 's/x/claude -p hi/;e'", True),
        ("sed 's/x/claude -p hi/;e' <<< x", True),
        ("echo x | gsed -e 's/x/claude -p hi/' -e e", True),
        ("printf x | sed 'y/x/c/;e'", True),
        (r"printf 'x\nclaude -p hi\n' | sed 'N;s/x\n//;e'", True),
        ("echo x | sed 's/x/claude -p hi/;h;x;e'", True),
        ("echo x | sed 's/x/claude -p hi/;H;G;e'", True),
        ("echo x | sed -n 's/x/claude -p hi/;e;p'", True),
        ("echo x | sed '1{s/x/claude -p hi/;e\n}'", True),
        ("echo x | sed -z 's/x/claude -p hi/;e'", True),
        ("echo x | sed --expression='s/x/claude -p hi/' --expression=e", True),
        ("echo x | sed 's/x/claude -p hi/M;e'", True),
        ("echo 'claude -p hi' | sed 'h;g;e'", True),
        ("echo 'claude -p hi' | sed 'x;G;e'", True),
        ("echo 'claude -p hi' | sed 'n;e'", True),
        ("echo 'claude -p hi' | sed 'D;e'", True),
        ("echo claude | sed 's/.*/& -p hi/e'", True),
        (r"echo claude | sed 's/\(.*\)/\1 -p hi/e'", True),
        ("echo hi | sed e", False),
        ("sed e <<< 'echo hi'", False),
        ("echo x | sed 's/x/claude -p hi/'", False),
        ("sed 's/^.*$/echo hi/e' f", False),
    ],
)
def test_sed_pattern_execution_after_modification(command: str, blocked: bool) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is blocked


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("function my-f { claude -p hi; }; my-f", True),
        ("my-f() { claude -p hi; }", True),
        ("f.x() { claude -p hi; }; f.x", True),
        ("function f:g { claude -p hi; }; f:g", True),
        ("() { claude -p hi; }", True),
        ("() { claude -p hi; } a b", True),
        ("function { claude -p hi; }", True),
        ("repeat 1 { claude -p hi; }", True),
        ("for i (1) { claude -p hi; }", True),
        ("function f g { claude -p hi; }; g", True),
        ("f g() { claude -p hi; }; g", True),
        ("() { echo 'claude -p hi'; } | bash", True),
        ("my-f() { bash; }; echo 'claude -p hi' | my-f", True),
        ("my-f() { echo 'claude -p hi'; }; my-f | bash", True),
        ("my-f() { echo hi; }; my-f", False),
        ("function f g { echo hi; }; g", False),
        ("{ echo {; echo hi; } | bash", False),
        ("{ echo '}'; echo hi; }", False),
    ],
)
def test_final_spec_function_headers(command: str, blocked: bool) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is blocked


@pytest.mark.parametrize(
    ("leaf", "producer", "blocked"),
    [("cat", "echo hi", False), ("bash", "echo 'claude -p hi'", True), ("f0", "echo hi", False)],
)
def test_function_consumer_resolution_is_linear(
    monkeypatch: pytest.MonkeyPatch, leaf: str, producer: str, blocked: bool
) -> None:
    prepare = _prepare
    calls = 0

    def counted_prepare(command: str, depth: int, *, data: bool = False) -> tuple[str, list[str]]:
        nonlocal calls
        calls += 1
        assert calls <= 16 * 12
        return prepare(command, depth, data=data)

    monkeypatch.setattr(provider_launch_guard, "_prepare", counted_prepare)
    definitions = "; ".join(
        f"f{index}() {{ f{index + 1}; f{index + 1}; f{index + 1}; }}" for index in range(12)
    )
    command = f"{definitions}; f12() {{ {leaf}; }}; {producer} | f0"
    assert blocks_direct_provider_launch("Bash", {"command": command}) is blocked
    assert calls <= 16 * 12


@pytest.mark.parametrize(
    "command",
    [
        "function f { claude -p hi; }; f",
        "function f() { claude -p hi; }; f",
        "f () { claude -p hi; }; f",
        "coproc { claude -p hi; }",
        "coproc claude -p hi",
        "for i in $(claude -p hi); do :; done",
        "f() { echo 'import os; os.system(\"claude -p hi\")'; }; f > >(tee >(bash) >(python3))",
        "f() { echo 'import os; os.system(\"claude -p hi\")'; }; f | tee >(bash) | python3",
        'bash -c \'echo "import os; os.system(\\"claude -p hi\\")"\' > >(tee >(bash) >(python3))',
        "bash <<'EOF' | bash\necho 'claude -p hi'\nEOF",
        "bash <<'EOF' | python3\necho 'import os; os.system(\"claude -p hi\")'\nEOF",
        "f() { bash; }; f < <(echo 'claude -p hi')",
        "f() { bash; }; f <<< 'claude -p hi'",
        "sed 's/x/claude -p hi/e' f",
        "echo 'claude -p hi' | sed e",
        "gsed '1e claude -p hi' f",
        "sed e input.txt",
        "sed 's/x/y/e' input.txt",
        "sed 'N;s/^.*$/echo hi/e' f",
        "sed 'H;s/^.*$/echo hi/e' f",
        "sed 'G;s/^.*$/echo hi/e' f",
        "sed 's/^.*$/echo hi/eM' f",
        "sed 's/^.*$/echo hi/Me' f",
        "sed 's/^.*$/&/e' f",
        r"sed 's/^.*$/\1/e' f",
        r"sed 's/^.*$/echo\nhi/e' f",
        "sed -z 's/^.*$/echo hi/e' f",
        "sed -e N -e 's/^.*$/echo hi/e' f",
    ],
)
def test_provider_review_correction_blocks_launches(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "for claude in 1; do echo $claude; done",
        "for codex in a b; do echo $codex; done",
        "select grok in a; do break; done",
        "function f { echo hi; }; f",
        "f () { echo hi; }; f",
        "sed '1e echo hi' f",
        "gsed 's/x/y/' f",
        "echo hi | sed e",
        "sed 's/^.*$/echo hi/e' f",
        "sed 'N;s/^.*$/echo hi/' f",
        "sed 'H;s/^.*$/echo hi/' f",
        "sed 'G;s/^.*$/echo hi/' f",
        "sed 's/^.*$/echo hi/M' f",
        "sed e <<< 'echo hi'",
    ],
)
def test_provider_review_correction_allows_data(command: str) -> None:
    assert not blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "source <(echo 'claude -p hi')",
        ". <(echo 'claude -p hi')",
        "echo 'claude -p hi' | source /dev/stdin",
        "source /dev/stdin <<< 'claude -p hi'",
        ". /dev/stdin <<< 'claude -p hi'",
        "source /dev/stdin <<'EOF'\nclaude -p hi\nEOF",
        "{ bash; } < <(echo 'claude -p hi')",
        "{ bash; } <<< 'claude -p hi'",
        "{ bash; } <<'EOF'\nclaude -p hi\nEOF",
        "echo 'claude -p hi' | bash /dev/stdin",
        "bash /dev/stdin < <(echo 'claude -p hi')",
        "bash /dev/stdin <<< 'claude -p hi'",
        "bash -c bash < <(echo 'claude -p hi')",
        "echo 'claude -p hi' | bash -c bash",
        "bash =(echo 'claude -p hi')",
        "zsh -c \"bash =(echo 'claude -p hi')\"",
        "bash -c 'echo claude -p hi' | bash",
        "sh -c 'echo claude -p hi' | sh",
        "echo -n 'claude -p hi' | bash",
        "bash < <(bash <(echo 'echo claude -p hi'))",
        "sed '1e claude -p hi' src/x.py",
        "sed -e '1e claude -p hi' src/x.py",
        "if true; then echo 'claude -p hi'; fi | bash",
        "while true; do echo 'claude -p hi'; break; done | bash",
        "case x in x) echo 'claude -p hi';; esac | bash",
        "for i in 1; do echo 'claude -p hi'; done > >(bash)",
        "for i in 1; do echo 'claude -p hi'; done | tee >(bash)",
        "if true; then echo 'claude -p hi'; fi > >(bash)",
        "while true; do echo 'claude -p hi'; break; done > >(bash)",
        "case x in x) echo 'claude -p hi';; esac > >(bash)",
        "f() { echo 'claude -p hi'; }; f | bash",
        "f() { echo 'claude -p hi'; }; f > >(bash)",
        "{ : }; echo 'claude -p hi'; } | bash",
        "{ : }; echo 'claude -p hi'; } > >(bash)",
        "bash <&3 3< <(echo 'claude -p hi')",
        "bash 0<&3 3< <(echo 'claude -p hi')",
        "bash <&3 3<<< 'claude -p hi'",
        "bash 0<&3 3<<< 'claude -p hi'",
        "bash <&3 3<<'EOF'\nclaude -p hi\nEOF",
        "bash 0<&3 3<<'EOF'\nclaude -p hi\nEOF",
    ],
)
def test_provider_consumer_gaps_block_launches(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "diff <(ls a) <(ls b)",
        "bash < <(echo hi)",
        "source ./env.sh",
        "{ echo hi; } < file",
        "sed '1s/a/b/' f",
        "{ echo hi; } > >(cat)",
        "{ : }; echo hi; } > >(bash)",
        "{ echo {; echo hi; } | bash",
        "cat <&3 3< <(echo hi)",
        "source <(echo 'export A=1')",
        "source /dev/stdin <<< 'export A=1'",
        "{ bash; } <<< 'echo hi'",
        "bash /dev/stdin < <(echo 'echo hi')",
        "echo 'echo hi' | bash -c bash",
        "bash =(echo 'echo hi')",
        "bash -c 'echo echo hi' | bash",
        "sed '1e echo hi' f",
        "if true; then echo hi; fi | bash",
        "while true; do echo hi; break; done | bash",
        "case x in x) echo hi;; esac > >(bash)",
        "for i in 1; do echo hi; done | tee >(bash)",
        "f() { echo hi; }; f | bash",
        "bash <&3 3<<< 'echo hi'",
    ],
)
def test_provider_consumer_gap_data_controls_stay_allowed(command: str) -> None:
    assert not blocks_direct_provider_launch("Bash", {"command": command})


def test_brace_heavy_command_preserves_group_results() -> None:
    command = "{ echo hi; }; " * 2000
    assert not blocks_direct_provider_launch("Bash", {"command": command})
    for brace_command, blocked in (
        ("{ claude -p hi; }", True),
        ("{ { claude -p hi; }; }", True),
        ("{ echo 'claude -p hi'; } | bash", True),
        ("{ echo '}'; echo hi; }", False),
        ("{ echo {; echo hi; } | bash", False),
    ):
        assert blocks_direct_provider_launch("Bash", {"command": brace_command}) is blocked


@pytest.mark.parametrize(
    "shape", ["{ X; } | bash", "bash <(X)", "{ X; } > >(bash)", "{ X; } | python3"]
)
def test_nested_chain_evaluation_count_is_linear(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    prepare = _prepare
    calls = 0

    def counted_prepare(command: str, depth: int, *, data: bool = False) -> tuple[str, list[str]]:
        nonlocal calls
        calls += 1
        assert calls <= 16 * 12
        return prepare(command, depth, data=data)

    monkeypatch.setattr(provider_launch_guard, "_prepare", counted_prepare)
    command = "echo hi"
    for _ in range(12):
        command = shape.replace("X", command)
    assert not blocks_direct_provider_launch("Bash", {"command": command})
    assert calls <= 16 * 12

    calls = 0
    command = "claude -p hi"
    for _ in range(12):
        command = shape.replace("X", command)
    assert blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize("outer", ["{ X; } | bash", "bash <(X)", "{ X; } > >(bash)"])
def test_memo_preserves_output_consumer_through_unpiped_groups(outer: str) -> None:
    command = "echo 'claude -p hi'"
    for _ in range(11):
        command = "{ X; }".replace("X", command)
    command = outer.replace("X", command)
    assert blocks_direct_provider_launch("Bash", {"command": command})


def test_blocked_memo_is_scoped_per_classification() -> None:
    assert not blocks_direct_provider_launch("Bash", {"command": "echo hi"})
    assert provider_launch_guard._blocked_memo.get() is None


@pytest.mark.parametrize(
    "command",
    [
        "bash <(echo 'claude -p hi')",
        "bash \"\"<(echo 'claude -p hi')",
        "bash +x <(echo 'claude -p hi')",
        "bash +e <(echo 'claude -p hi')",
        "bash <(cat <<< 'claude -p hi')",
        "bash < <(cat <<< 'claude -p hi')",
        "python3 <(cat <<< 'import os; os.system(\"claude -p hi\")')",
        "bash <(cat 0<<< 'claude -p hi')",
        "cat <<< 'claude -p hi' | bash",
        "bash <(echo $'\\claude -p hi')",
        "bash < <(echo $'\\claude -p hi')",
        "bash <(printf $'\\claude -p hi')",
        "bash <(cat <(echo 'claude -p hi'))",
        "cat <(echo 'claude -p hi') | bash",
        "bash <(cat < <(echo 'claude -p hi'))",
        "python3 <(cat <(echo 'import os; os.system(\"claude\")'))",
        "bash < <(echo 'claude -p hi')",
        "bash 0< <(echo 'claude -p hi')",
        "python3 < <(echo 'import os; os.system(\"claude -p hi\")')",
        "python3 <(printf '%s' 'import os; os.system(\"codex exec hi\")')",
        "env bash -- <(printf '%s' 'codex exec hi')",
        "bash <(echo 'claude -p hi'; echo 'true')",
        "bash <(cat <<'EOF'\nclaude -p hi\nEOF\n)",
        "bash <(bash <(echo 'claude -p hi'))",
        "bash <(sh -c \"echo 'claude -p hi'\")",
        'python3 <(sh -c "echo \'import os; os.system(\\"claude\\")\'")',
    ],
)
def test_process_substitution_blocks_provider_launch(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo $(case y in y) codex exec hi;; esac)",
        "cat <(case y in y) codex exec hi;; esac)",
        "echo $(true; case y in y) codex exec hi;; esac)",
        "echo $(echo $(case y in y) codex exec hi;; esac))",
    ],
)
def test_grouped_case_blocks_provider_launch(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "diff <(ls a) <(ls b)",
        "cat <(echo 'claude -p hi')",
        "bash <(echo 'echo harmless')",
        "python3 <(echo 'print(\"claude -p hi\")')",
        "bash script.sh <(echo 'claude -p hi')",
        "bash -c 'echo harmless' <(echo 'claude -p hi')",
        "bash -s <(echo 'claude -p hi')",
        "bash 2< <(echo 'claude -p hi')",
        "bash --rcfile <(echo 'echo harmless') script.sh",
        "python3 -X <(echo 'import os; os.system(\"claude\")') script.py",
        "python3 -c 'print(1)' < <(echo 'import os; os.system(\"claude\")')",
        "echo '<(claude -p hi)'",
        "bash +e <(cat <<< 'echo harmless')",
        "bash <(cat input.txt <<< 'claude -p hi')",
        "bash <(cat 2<<< 'claude -p hi')",
        "bash <(cat <(echo 'echo harmless'))",
        "cat <(cat <(echo 'claude -p hi'))",
    ],
)
def test_process_substitution_data_stays_allowed(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is False


@pytest.mark.parametrize(
    "command",
    [
        "echo 'claude -p hi' > >(bash)",
        "printf '%s' 'codex exec hi' > >(sh)",
        "echo 'claude -p hi' 1> >(env bash)",
        "echo 'import os; os.system(\"claude -p hi\")' > >(python3)",
        "cat <<'EOF' > >(bash)\nclaude -p hi\nEOF\n",
    ],
)
def test_output_process_substitution_blocks_provider_launch(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo 'claude -p hi' > >(cat)",
        "echo 'echo harmless' > >(bash)",
        "echo 'import os; os.system(\"claude\")' > >(python3 -c 'print(1)')",
    ],
)
def test_output_process_substitution_data_stays_allowed(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is False


@pytest.mark.parametrize("redirect", [">>", "1>>", "&>", ">&", "&>>", ">|", "1>|"])
@pytest.mark.parametrize(
    ("payload", "consumer"),
    [
        ("claude -p hi", "bash"),
        ('import os; os.system("claude -p hi")', "python3"),
    ],
)
def test_output_process_substitution_redirect_equivalents(
    redirect: str, payload: str, consumer: str
) -> None:
    command = f"echo '{payload}' {redirect} >({consumer})"
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize("redirect", ["2>", "2>>", "3>", "10>"])
def test_output_process_substitution_other_descriptors_are_inspected(redirect: str) -> None:
    command = f"echo 'claude -p hi' {redirect} >(bash)"
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo 'claude -p hi' 3> >(bash) 1>&3",
        "echo 'claude -p hi' 2> >(bash) >&2",
        "echo 'claude -p hi' > >(true; bash)",
        "echo 'claude -p hi' > >(tee >(bash))",
        "echo 'claude -p hi' | tee >(bash)",
        "echo 'claude -p hi' | tee >(bash) >/dev/null",
        "exec > >(bash); echo 'claude -p hi'",
        "cat <<'EOF' >> >(bash)\nclaude -p hi\nEOF",
        "{ echo 'claude -p hi'; } > >(bash)",
        "(echo 'claude -p hi') > >(bash)",
        "{ echo 'claude -p hi'; } | bash",
        "echo 'claude -p hi' | (true; bash)",
    ],
)
def test_output_process_substitution_forwarding(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "> >(cat)",
        "echo 'echo harmless' > >(bash)",
        "echo hi > >(tee log.txt)",
        "make 2>&1 > >(tee build.log)",
        "exec > >(tee -a log.txt)",
        "python3 run.py > >(tee out.log) 2> >(tee err.log >&2)",
        ">> >(cat)",
    ],
)
def test_output_process_substitution_forwarding_data_stays_allowed(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is False


@pytest.mark.parametrize(
    "command",
    [
        "case x in (x) claude -p hi;; esac",
        "case x in (a|x) claude -p hi;; esac",
        "case x in a) echo;; (x) claude -p hi;; esac",
        'echo "$(case x in (x) claude -p hi;; esac)"',
        "case x in\n(x) claude -p hi\n;;\nesac",
        "case x in (x) echo 'claude -p hi' | bash;; esac",
    ],
)
def test_case_pattern_parentheses_preserve_provider_commands(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


def test_case_pattern_parentheses_with_harmless_commands_stay_allowed() -> None:
    assert (
        blocks_direct_provider_launch("Bash", {"command": "case $x in (a) echo a;; esac"}) is False
    )


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'echo ${x/)(/-}'",
        "bash -c '(echo ${x/)(/-})'",
        "bash -c 'echo \")(claude)\"'",
        "bash -c 'case x in x)(echo ${x/)(/-});; esac'",
    ],
)
def test_literal_parentheses_in_parameter_data_stay_allowed(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is False


@pytest.mark.parametrize(
    "body",
    ["echo ${x:-$(claude)}", "echo ${x:-`claude`}", 'echo "${x:-${y:-$(claude)}}"'],
)
def test_parameter_data_still_inspects_executable_substitutions(body: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": f"bash -c '{body}'"}) is True


@pytest.mark.parametrize("command", ["echo $${", "bash -c 'echo $${'"])
def test_pid_parameter_with_literal_brace_is_data(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is False


def test_pid_parameter_with_literal_brace_still_inspects_substitution() -> None:
    assert blocks_direct_provider_launch("Bash", {"command": "bash -c 'echo $${x:-$(claude)}'"})


@pytest.mark.parametrize(
    "command",
    [
        "{ echo 'import os; os.system(\"claude -p hi\")'; } > >(tee >(bash) >(python3))",
        "(echo 'import os; os.system(\"claude -p hi\")') > >(tee >(bash) >(python3))",
        "{ echo 'import os; os.system(\"claude -p hi\")'; } | tee >(bash) | python3",
    ],
)
def test_grouped_producers_inspect_each_executable_consumer(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "bash 0<<<'claude -p hi'",
        "sh 0<<<'codex exec hi'",
        "python3 0<<<'import os; os.system(\"claude -p hi\")'",
    ],
)
def test_numbered_here_string_blocks_provider_launch(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo $$'\\' ; codex exec hi ; echo '\\'",
        "echo $$'\\'' ; codex exec hi ; echo \\'",
        "echo $(echo $$'\\') ; codex exec hi ; echo ')'\\'",
    ],
)
def test_dollar_pair_quote_blocks_provider_launch(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command})


def test_dollar_pair_quote_literal_allows_provider_guard() -> None:
    assert not blocks_direct_provider_launch("Bash", {"command": "echo $$'.txt'"})


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "options",
    [
        "-a /tmp/input",
        "-a/tmp/input",
        "-0a /tmp/input",
        "-0a/tmp/input",
        "--arg-file /tmp/input",
        "--arg-file=/tmp/input",
        "--arg-f /tmp/input",
        "--arg-f=/tmp/input",
        "--max-chars 4096",
        "--max-chars=4096",
        "--max-char 4096",
        "--max-char=4096",
        "--process-slot-var SLOT",
        "--process-slot-var=SLOT",
        "--process-slot SLOT",
        "--process-slot=SLOT",
        "-I {} -R 2",
        "-I{} -R2",
        "-I {} -0R2",
        "-I {} -S 4096",
        "-I{} -S4096",
        "-I {} -0S4096",
        "--max-lines",
        "--max-lines=3",
    ],
)
@pytest.mark.parametrize(("target", "blocked"), [("codex exec", True), ("echo codex exec", False)])
def test_xargs_required_operands(tool_name: str, options: str, target: str, blocked: bool) -> None:
    key = "command" if tool_name == "Bash" else "cmd"
    assert blocks_direct_provider_launch(tool_name, {key: f"xargs {options} {target}"}) is blocked


SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
RULE = SHARED / "workflows/rules/worker-safety/block-direct-provider-launch.yaml"
SESSION = "abababab-0000-4000-8000-000000000001"


@pytest.mark.parametrize("error", [MemoryError, RecursionError])
@pytest.mark.parametrize("target", ["ast.parse", "_python_skeleton"])
def test_python_parse_resource_limit_fails_closed(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception], target: str
) -> None:
    def fail_parse(source: object, *args: object, **kwargs: object) -> None:
        raise error("parser resource limit")

    with monkeypatch.context() as patch:
        patch.setattr(f"gobby.hooks.provider_launch_guard.{target}", fail_parse)
        blocked = blocks_direct_provider_launch(
            "exec_command", {"cmd": "python3 -c 'import os; os.system(\"true\")'"}
        )
    assert blocked


@pytest.mark.parametrize(
    "command,blocked",
    [
        pytest.param(
            "uv run python - <<'MM23659_CLOSE_REPORT'\n"
            "from pathlib import Path\n"
            "Path('/tmp/provider-report.json').write_text("
            + repr("codex exec --sandbox danger-full-access " + "x" * 131_072)
            + ")\nMM23659_CLOSE_REPORT\n",
            False,
            id="large-report-data",
        ),
        pytest.param(
            "uv run python -c 'from pathlib import Path; "
            'Path("/tmp/provider-report.txt").write_text("codex exec ' + "x" * 131_072 + '")'
            "'",
            False,
            id="large-inline-report-data",
        ),
        ("uv run python report.py -c 'import subprocess; subprocess.run([\"codex\"])'", False),
        *[
            pytest.param(
                "uv run python - <<'PY'\n"
                f"{imports}\nfrom pathlib import Path\n"
                "Path('/tmp/report.txt').write_text('codex exec hi')\nPY\n",
                False,
                id=f"dotted-report-{index}",
            )
            for index, imports in enumerate(
                (
                    "import os\nimport os.path",
                    "import urllib.request\nimport urllib.parse",
                    "import xml.etree.ElementTree\nimport xml.dom.minidom",
                )
            )
        ],
        *[
            pytest.param(
                f"uv run python - <<'PY'\n{body}\nPY\n",
                True,
                id=f"round2-launch-{index}",
            )
            for index, body in enumerate(
                (
                    "import os.path\nos.system('codex exec hi')",
                    "import asyncio.subprocess\nasyncio.create_subprocess_shell('codex exec hi')",
                    "import os\nos.system('codex exec %s' % x)",
                    "import os\nos.system('codex exec {}'.format(x))",
                    "import os\nos.system(' '.join(['codex', 'exec', x]))",
                    "import os, shlex\nos.system(shlex.join(['codex', 'exec', x]))",
                )
            )
        ],
        *[
            pytest.param(
                f"uv run python {option} <<'PY'\n"
                "import subprocess\nsubprocess.run(['codex'])\nPY\n",
                False,
                id=f"query-{option}",
            )
            for option in ("-V", "-h", "--version", "--help", "-IV")
        ],
        pytest.param(
            "uv run python -Ic 'import subprocess; subprocess.run([\"codex\"])'",
            True,
            id="clustered-inline-code",
        ),
        *[
            pytest.param(
                f"uv run python - <<'PY'\nimport subprocess, os, sys\n{body}\nPY\n",
                True,
                id=f"review-{index}",
            )
            for index, body in enumerate(
                (
                    'subprocess.run(["claude", "-p", p])',
                    'subprocess.run(["codex", "exec", *sys.argv])',
                    'subprocess.run(f"codex exec {p}", shell=True)',
                    'subprocess.run("codex exec " + p, shell=True)',
                    'os.system(f"claude -p {p}")',
                    "import json as j; import sys as j; "
                    'from subprocess import run; run(["codex", "exec", "hi"])',
                    'subprocess.run("codex exec hi", shell=1)',
                    'subprocess.run("codex exec hi", shell=use_shell)',
                )
            )
        ],
        *[
            pytest.param(command, True, id=f"python-channel-{index}")
            for index, command in enumerate(
                (
                    "echo 'import subprocess; subprocess.run([\"codex\"])' | python3",
                    "python3 <<< 'import subprocess; subprocess.run([\"codex\"])'",
                    "python3 --check-hash-based-pycs always - <<'PY'\n"
                    'import subprocess; subprocess.run(["codex"])\nPY\n',
                    "uv run python3.13 <<'PY'\n"
                    'import subprocess; subprocess.run(["codex"])\nPY\n',
                )
            )
        ],
        ("python3 -c 'import subprocess; subprocess.run([cmd, \"exec\"])'", False),
        ("python3 -c 'import subprocess; subprocess.run(\"codex exec\", shell=False)'", False),
        ("uv run python -c 'from subprocess import run as launch; launch([\"claude\"])'", True),
        ("uv run python -c 'import subprocess; subprocess.run(args=[\"codex\"])'", True),
        (
            'uv run python -c \'import subprocess; subprocess.run(["data"], executable="codex")\'',
            True,
        ),
        ("uv run python -c 'import subprocess; subprocess.run(\"true; codex\", shell=True)'", True),
        pytest.param(
            "uv run python -c 'import subprocess; "
            'subprocess.run("codex", shell=True, executable="/bin/bash")\'',
            True,
            id="shell-executable-override",
        ),
        pytest.param(
            "uv run python -c 'import subprocess; "
            'subprocess.run(["codex exec", "unused"], shell=True)\'',
            True,
            id="shell-list-command",
        ),
        pytest.param(
            "uv run python -c 'import subprocess; "
            'subprocess.run(["printf", "codex"], shell=True)\'',
            False,
            id="shell-list-data",
        ),
        ('uv run python -c \'import subprocess; subprocess.run(["printf", "codex exec"])\'', False),
        ('uv run python -c \'import subprocess; subprocess.run(["codex", "--help"])\'', False),
        (
            'uv run python -c \'import asyncio; asyncio.create_subprocess_exec("codex", "--help")\'',
            False,
        ),
        (
            "uv run python <<'PY'\n"
            "from pathlib import Path\n"
            "Path('/tmp/provider-report.json').write_text("
            '\'{"command": "codex exec --sandbox danger-full-access"}\')\n'
            "PY\n",
            False,
        ),
        (
            "uv run python -c 'from pathlib import Path; "
            'Path("/tmp/provider-report.txt").write_text("claude -p hi")'
            "'",
            False,
        ),
        ("codex exec hi", True),
        ("true && claude -p hi", True),
        ("true; codex exec hi", True),
        (
            "uv run python <<'PY'\n"
            "import subprocess\n"
            "subprocess.run(['codex', 'exec', 'hi'])\n"
            "PY\n",
            True,
        ),
        ("uv run python -c 'import subprocess; subprocess.run([\"codex\"])'", True),
    ],
)
@pytest.mark.asyncio
async def test_interpreter_provider_launch_rule(
    hub_db: HubDatabase, command: str, blocked: bool
) -> None:
    synced = sync_rule_file(hub_db, RULE, tag="gobby")
    assert synced["success"]
    row = RuleDefinitionManager(hub_db).get_by_name("block-direct-provider-launch")
    assert row is not None and row.enabled
    template = yaml.safe_load(RULE.read_text())["rules"]["block-direct-provider-launch"]
    assert row.description == template["description"]
    for field in ("event", "when", "effects"):
        assert row.definition_json[field] == template[field]
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "exec_command", "tool_input": {"cmd": command}},
    )
    result = await RuleEngine(hub_db).evaluate(event, session_id=SESSION, variables={})
    assert (result.decision == "block") is blocked
    if blocked:
        assert result.reason is not None and "block-direct-provider-launch" in result.reason


@pytest.mark.parametrize(
    ("command", "blocked"),
    [("codex exec smoke", True), ("codex --help", False), ("git status --short", False)],
)
async def test_native_codex_command_alias_with_command_pattern_rules(
    hub_db: HubDatabase, command: str, blocked: bool
) -> None:
    for path in (RULE, SHARED / "workflows/rules/task-enforcement/block-gobby-tasks-cli.yaml"):
        assert sync_rule_file(hub_db, path, tag="gobby")["success"]
    agent_body = yaml.safe_load((SHARED / "workflows/agents/default.yaml").read_text())
    AgentDefinitionManager(hub_db).create(
        name="default", definition_json=agent_body, source="custom"
    )
    event = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "session_id": SESSION,
                "tool_name": "exec_command",
                "tool_input": {"cmd": command},
            },
        }
    )
    assert event is not None
    assert event.data["tool_input"]["command"] == command
    assert event.data["_raw_tool_input"] == {"cmd": command}
    result = await RuleEngine(hub_db).evaluate(
        event, session_id=SESSION, variables={"_agent_type": "default", "is_spawned_agent": False}
    )
    assert (result.decision == "block") is blocked
    assert "block-gobby-tasks-cli" not in (result.reason or "")


@pytest.mark.parametrize("provider", ["codex", "claude", "droid", "grok", "agy"])
@pytest.mark.parametrize("prefix", ["", "/opt/bin/", "'/some path/bin/"])
@pytest.mark.parametrize(
    "args", ["", " exec hi", " resume", " -p hi", " unknown", " exec prompt --help"]
)
def test_provider_launches(provider: str, prefix: str, args: str) -> None:
    executable = prefix + provider + ("'" if prefix.startswith("'") else "")
    assert blocks_direct_provider_launch("Bash", {"command": executable + args})


@pytest.mark.parametrize(
    "command",
    [
        "codex --help",
        "claude -h",
        "droid --version",
        "grok --help",
        "agy --help",
        "codex help",
        "codex -V",
        "codex login status",
        "codex help login",
        "codex help login status",
        "codex exec --help",
        "codex login status -h",
        "claude auth status --help",
        "claude auth status --json",
        "claude auth status --text",
        "droid help exec",
        "grok agent --help",
        "grok version",
        "grok v",
        "agy help mcp",
        "claude auth status",
        "claude mcp add --help",
        "codex mcp list --help",
        "grok models --help",
        "claude auth login --help",
        "claude help",
        "env A=b /opt/bin/codex login status",
        "command -- codex --help",
        "codex --help && claude auth status",
        "bash -lc 'codex login status'",
        "echo codex exec",
        "printf '%s\\n' 'codex exec --sandbox danger-full-access'",
        "echo '$(codex exec)'",
        "echo '`claude -p hi`'",
        'echo "codex exec"',
        "echo \\$\\(codex\\)",
        "git status # codex exec '",
        "# codex exec\nprintf done",
        "command -v codex",
        "command -V /opt/bin/claude",
        "command -p -v codex",
        "command -pv codex",
        "claude -v",
        "droid -v",
        "grok -v",
        "type codex",
        "which codex",
        "cat <<'EOF'\ncodex exec\n$(claude -p hi)\n' malformed documentation\nEOF\n",
        "cat <<EOF\ncodex exec\nEOF\n",
        "cat <<EOF\n<(codex)\nEOF\n",
        "bash script.sh <<'EOF'\ncodex exec\nEOF\n",
        "cat <<'EOF' |\n$(codex exec)\nEOF\n tee docs\n",
        "echo hello | sh",
        "bash -c 'printf hello' <<'EOF'\ncodex\nEOF\n",
        "echo '# codex'; printf '%s' '# claude'",
        "echo \"$(printf '%s' 'codex exec')\"",
        "git commit -m \"$(cat <<'EOF'\nfix: the watchdog's hook (1M context\nEOF\n)\"",
        'git commit -m "$(cat <<EOF\nit\'s done\nEOF\n)"',
        "x=\"$(cat <<-'EOF' |\n\tit's (text\n\tEOF\n tr a b\n)\"",
        # ANSI-C quoting escapes its apostrophe; the string is data (#23134).
        "echo $'it\\'s'",
        # `\c` does not consume the closing quote; the word is data.
        "echo $'\\c'Z",
        # Either dialect's decoding of an echo argument is still data.
        "echo $'\\claude -p hi'",
    ],
)
def test_administration_and_documentation(command: str) -> None:
    assert not blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "codex exec prompt --help",
        "codex help unknown",
        "claude auth status --text --resume",
        "grok agent prompt --help",
        "agy mcp unknown --help",
        # A prompt cannot pose as a help word: unknown words end the path, and
        # a leaf subcommand takes a prompt, not `help`.
        "claude please help",
        "codex exec help",
        "claude mcp add --scope user --help",
    ],
)
def test_help_does_not_exempt_launch_operands(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "true && codex exec hi",
        "printf hi | claude -p hi",
        "false || droid",
        "true; grok",
        "true & droid",
        "echo done\nagy",
        "A=b codex",
        "env -i A=b codex exec hi",
        "env -u HOME -- codex",
        "env --chdir /tmp codex",
        "command codex",
        "builtin exec codex",
        "exec -a harmless /opt/bin/codex",
        "nohup codex",
        "nice -n 5 codex",
        "timeout -k 2 5 codex",
        "sudo -u nobody codex",
        # Every sudo option that takes a value consumes its operand (#23134).
        "sudo -D /tmp codex",
        "sudo -R /srv codex",
        "sudo -U nobody codex",
        "sudo --close-from 3 codex",
        "sudo --command-timeout 5 codex",
        "sudo -nD /tmp codex",
        "sudo -D/tmp codex",
        "sudo --chdir=/tmp codex",
        "/usr/bin/env -- codex",
        "time -p codex",
        "env -S 'codex exec'",
        "env --split-string='codex exec'",
        "env -iS 'codex exec'",
        "env -Scodex",
        "env -P /usr/bin codex",
        "env -a name codex",
        "setsid --wait codex",
        "stdbuf -o L codex",
        "xargs -I '{}' codex exec '{}'",
        "eval 'codex exec'",
        "eval codex exec",
        "bash -lc 'codex exec hi'",
        "sh -c 'env X=1 claude -p hi'",
        "(codex)",
        "( codex exec )",
        "if true; then codex; fi",
        "while true; do codex; done",
        "{ codex; }",
        'echo "$(codex exec hi)"',
        "A=$(codex exec hi) echo done",
        "echo `codex exec hi`",
        "echo $(printf '%s' \"$(claude -p hi)\")",
        "cat <(codex exec hi)",
        "sh <<'EOF'\ncodex exec hi\nEOF\n",
        "bash -s <<EOF\nclaude -p hi\nEOF\n",
        "bash -o errexit <<'EOF'\ncodex\nEOF\n",
        "bash <<-'EOF'\n\tgrok\n\tEOF\n",
        "cat <<EOF\n'$(codex exec hi)'\nEOF\n",
        "cat <<EOF\n`claude -p hi`\nEOF\n",
        "cat <<'EOF' | sh\ncodex exec\nEOF\n",
        "cat <<'EOF' |\ncodex\nEOF\n sh\n",
        "sh <<< 'codex exec'",
        "uv run python -c 'import subprocess; subprocess.run([\"codex\"])'",
        "printf '%s' 'codex exec' | sh",
        "codex --help; codex exec hi",
        "codex login status --unknown",
        "claude auth login",
        "droid auth status",
        "grok auth status",
        "agy auth status",
        "claude -V",
        "GOBBY_ALLOW_DIRECT_PROVIDER=1 codex",
        "codex > /dev/null",
        "2>/dev/null codex",
        "codex --help $(claude -p hi)",
        "echo done # codex --help\nclaude -p hi",
        "x=\"$(cat <<'EOF'\nit's text\nEOF\n)\"; claude -p hi",
        'x="$(cat <<EOF\nit\'s $(claude -p hi)\nEOF\n)"',
        "x=\"$(sh <<'EOF'\ncodex exec\nEOF\n)\"",
        "echo $(( 1 << EOF\n))\nclaude -p hi\nEOF\n))",
        "bash -c $'claude -p \\'hi\\''",
        "echo $'it\\'s'; claude -p hi",
        "bash -c -- 'claude -p hi'",
        # bash decodes C escapes inside `$'...'`, so each spells a separator or name.
        "bash -c $'echo hi\\nclaude -p hi'",
        "bash -c $'echo hi\\x0aclaude -p hi'",
        "bash -c $'echo hi\\012claude -p hi'",
        "bash -c $'echo hi\\cJclaude -p hi'",
        "bash -c $'\\x63laude -p hi'",
        # zsh keeps a decoded NUL, so `eval` still runs the command after it.
        "eval $'true\\0; claude -p hi'",
        "eval $'true\\x00; claude -p hi'",
        # zsh drops an unknown escape's backslash and reads `\C-j` as a newline.
        "$'\\claude' -p hi",
        "eval $'true\\C-jclaude -p hi'",
        # A named zsh decodes its script's escapes as zsh, whatever the outer shell.
        "zsh -c \"\\$'\\\\claude' -p hi\"",
    ],
)
def test_execution_contexts(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command})


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("env -S 'echo hi; codex exec'", False),
        ("env -iS 'echo hi; codex exec'", False),
        ("env -S echo 'hi; codex exec'", False),
        ("env -S '-i FOO=x echo hi; codex exec'", False),
        ("env -S '-i FOO=x codex exec'", True),
        (r"env -S 'codex\_exec'", True),
        ("env -S '\"codex\" exec'", True),
        ("env -S '\"codex exec\"'", False),
        (r"env -S 'codex\texec'", False),
        (r"env -S 'echo hi\c; codex exec'", False),
        (r"env -S '\"echo\_hi\" codex exec'", False),
        ("env -S '' codex exec", True),
        ("env -S '-S \"codex exec\"'", True),
        ("env -S 'codex exec \"'", True),
        ("env --split 'codex exec'", True),
        ("env --spl='codex exec'", True),
        ("env --env0-from /tmp/vars codex", True),
        ("env --quoting-style shell codex", True),
        ("sudo --chd /tmp codex", True),
        ("sudo --us root codex", True),
        ("sudo -a auth codex", True),
        ("sudo -c staff codex", True),
        ("sudo -r role codex", True),
        ("sudo -t type codex", True),
        ("sudo --auth-type auth codex", True),
        ("sudo --login-class staff codex", True),
        ("sudo --role role codex", True),
        ("sudo --type type codex", True),
    ],
)
def test_env_split_and_getopt_prefixes_preserve_launches(
    tool_name: str, command: str, blocked: bool
) -> None:
    key = "command" if tool_name == "Bash" else "cmd"
    assert blocks_direct_provider_launch(tool_name, {key: command}) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize("prefix", ["sudo --login", "sudo --login-class x", "sudo --login-c x"])
def test_sudo_exact_flag_precedes_value_prefix(tool_name: str, prefix: str) -> None:
    key = "command" if tool_name == "Bash" else "cmd"
    assert blocks_direct_provider_launch(tool_name, {key: prefix + " codex"}) is True


@pytest.mark.parametrize(
    "name",
    [
        "Bash",
        "bash",
        "shell",
        "run_command",
        "run_shell_command",
        "RunShellCommand",
        "ShellTool",
        "commandExecution",
        "exec_command",
    ],
)
def test_shell_aliases(name: str) -> None:
    assert blocks_direct_provider_launch(name, {"cmd": "codex exec hi"})


@pytest.mark.parametrize("tool_input", [None, {}, {"command": 3}, {"cmd": ""}])
def test_absent_command(tool_input: Any) -> None:
    assert not blocks_direct_provider_launch("Bash", tool_input)


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("echo $(true)#; codex exec hi", True),
        ("cat <<EOF &&\n# $(codex exec hi)\nEOF\ntrue", True),
        ("cat <<'EOF' &&\n# $(codex exec hi)\nEOF\ntrue", False),
        ("echo \"$(cat <<'EOF' &&\n)\n# $(codex exec hi)\nEOF\ntrue\n)\"", False),
    ],
)
def test_heredoc_continuation_and_substitution_word(command: str, blocked: bool) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command}) is blocked


def test_bounded_and_malformed_input() -> None:
    assert blocks_direct_provider_launch("Bash", {"command": "echo " + "x" * 131_072})
    assert blocks_direct_provider_launch("Bash", {"command": "echo 'unclosed"})
    assert blocks_direct_provider_launch("Bash", {"command": "echo \"$(cat <<'EOF'\nit's\n)\""})
    assert blocks_direct_provider_launch(
        "Bash", {"command": "echo " + "$(" * 30 + "codex" + ")" * 30}
    )
    assert not blocks_direct_provider_launch("spawn_agent", {"command": "codex exec hi"})


@pytest.mark.parametrize("agent", ["default", "developer", "merge-worker"])
@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.AGY,
    ],
)
@pytest.mark.parametrize(
    "command,tool_name,blocked",
    [
        ("codex exec --sandbox danger-full-access", "exec_command", True),
        ("codex login status", "exec_command", False),
        ("git status", "exec_command", False),
        ("codex exec --sandbox danger-full-access", "spawn_agent", False),
    ],
)
@pytest.mark.asyncio
async def test_real_engine_rule(
    hub_db: HubDatabase,
    agent: str,
    source: SessionSource,
    command: str,
    tool_name: str,
    blocked: bool,
) -> None:
    synced = sync_rule_file(hub_db, RULE, tag="gobby")
    assert synced["success"]
    row = RuleDefinitionManager(hub_db).get_by_name("block-direct-provider-launch")
    assert row is not None
    agent_body = yaml.safe_load((SHARED / f"workflows/agents/{agent}.yaml").read_text())
    AgentDefinitionManager(hub_db).create(name=agent, definition_json=agent_body, source="custom")
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION,
        source=source,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name, "tool_input": {"cmd": command}},
    )
    result = await RuleEngine(hub_db).evaluate(
        event,
        session_id=SESSION,
        variables={
            "_agent_type": agent,
            "allow_direct_provider_launch": True,
            "is_spawned_agent": False,
        },
    )
    assert row.enabled
    assert (result.decision == "block") is blocked
    if blocked:
        assert result.reason is not None
        assert "block-direct-provider-launch" in result.reason
        assert "gobby-agents:spawn_agent" in result.reason
        assert "extra_write_paths" in result.reason
        assert "write_paths_reason" in result.reason
