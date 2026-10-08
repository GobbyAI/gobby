"""Tests for the rules-engine guard around native task CLI mutations."""

from __future__ import annotations

import shlex
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

_ODD_PYTHON_SOURCE = "python3 - <<'PY'\nprint('it\\'s done')\nPY\n"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    sync_bundled_rules(database, get_bundled_rules_path())
    database.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return database


@pytest.fixture
def effect(db: HubDatabase) -> RuleEffect:
    manager = RuleDefinitionManager(db)
    row = manager.get_by_name("block-gobby-tasks-cli")
    assert row is not None

    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.event.value == "before_tool"
    assert body.resolved_effects[0].type == "block"
    return body.resolved_effects[0]


def _shell_event(tool_name: str, command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name, "tool_input": {"command": command}},
    )


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby tasks create --help",
        "gobby tasks update #1 --priority 1",
        "gobby tasks close #1",
        "gobby tasks claim #1",
    ],
)
def test_blocks_mutating_task_cli_commands(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby tasks list --ready",
        "gobby tasks ready",
        "gobby tasks blocked",
        "gobby tasks stats",
        "gobby tasks show #1",
        "gobby tasks backup --quiet",
        "gobby tasks expand validate-plan .gobby/plans/example.md",
        "gobby tasks validation_history #1",
        "gobby tasks doctor",
    ],
)
def test_allows_read_only_task_cli_commands(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # A quoted heredoc body is stdin data, not an invocation (#23134): the
        # phrase can sit at the start of a line inside a serialized string.
        'RTK_DISABLED=1 uv run python - <<\'PY\'\nDOC = """\ngobby tasks close\n"""\nPY',
        # A serialized evidence/documentation payload is data, not an invocation.
        'RTK_DISABLED=1 uv run python - <<\'PY\'\nPAYLOAD = \'{"content": "gobby tasks close", "task": "#1"}\'\nPY',
        # A quoted echo argument is prose.
        'echo "gobby tasks close 42"',
        # A wrapper keeps its command's word boundaries: the quoted -c source
        # is one data argument, not a `;`-separated invocation.
        'timeout 5 python -c "x = 1; gobby tasks close 42"',
        'uv run --with pyyaml python -c "x = 1; gobby tasks close 42"',
        # Isolated interpreter quotes must not turn literal task words into code.
        "cat <<EOF\n$(" + _ODD_PYTHON_SOURCE + ")\n$(echo 'gobby tasks close 1')\nEOF",
        "echo \"$(python3 - <<'PY'\nprint('say \"hi')\nPY\n)$(echo 'gobby tasks close 1')\"",
    ],
)
def test_allows_quoted_heredoc_and_string_data(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # An unquoted interpreter heredoc body executes: keep blocking.
        "uv run python - <<PY\ngobby tasks close 1\nPY",
        # A quoted heredoc piped to a shell executes: keep blocking.
        "bash -s <<EOF\ngobby tasks close 1\nEOF",
        # Each interpreter body has its own quote context. Python's escaped
        # apostrophe cannot hide a later invocation in the enclosing shell.
        "bash <<'EOF'\n" + _ODD_PYTHON_SOURCE + "gobby tasks close 1\nEOF",
        "sh <<'EOF'\n" + _ODD_PYTHON_SOURCE + "gobby tasks close 1\nEOF",
        "bash -s <<'EOF'\n" + _ODD_PYTHON_SOURCE + "gobby tasks close 1\nEOF",
        "bash -s <<'EOF'\nuv run python - <<'PY'\nprint('it\\'s done')\nPY\ngobby tasks close 1\nEOF",
        "sh <<'EOF'\nnode <<'JS'\nconsole.log('it\\'s')\nJS\ngobby tasks close 1\nEOF",
        "ssh host <<'EOF'\n" + _ODD_PYTHON_SOURCE + "gobby tasks close 1\nEOF",
        # Sibling bodies must not share quote context either.
        "bash 3<<'A' <<'B'\nit's\nA\ngobby tasks close 1\nB",
        "python3 - <<'A' | bash -s <<'B'\nprint('it\\'s done')\nA\ngobby tasks close 1\nB",
        # Keep bare, compound, and substitution execution controls.
        "bash <<'EOF'\ngobby tasks close 1\nEOF",
        "sh <<'EOF'\n\"gobby\" tasks close 1\nEOF",
        "bash <<'EOF' && echo done\n" + _ODD_PYTHON_SOURCE + "gobby tasks close 1\nEOF",
        "echo $(bash <<'EOF'\n" + _ODD_PYTHON_SOURCE + "gobby tasks close 1\nEOF\n)",
        "echo \"$(bash <<'EOF'\n" + _ODD_PYTHON_SOURCE + 'gobby tasks close 1\nEOF\n)"',
        # Command substitution inside double quotes still executes.
        'echo "$(gobby tasks close 42)"',
        # Substitutions in stdin data and double-quoted arguments execute in
        # separate quote contexts, including interpreter bodies with odd quotes.
        "cat <<EOF\n$(" + _ODD_PYTHON_SOURCE + ")\n$(gobby tasks close 1)\nEOF",
        "cat <<EOF\n$(python3 - <<'PY'\nprint('done')\nPY\n)\n$(gobby tasks close 1)\nEOF",
        "echo \"$(python3 - <<'PY'\nprint('say \"hi')\nPY\n)\" | gobby tasks close 1",
        "echo \"$(python3 - <<'PY'\nprint('done')\nPY\n)\" | gobby tasks close 1",
        "echo \"$(python3 - <<'PY'\nprint('say \"hi')\nPY\n)$(gobby tasks close 1)\"",
        "echo \"$(python3 - <<'PY'\nprint('done')\nPY\n)$(gobby tasks close 1)\"",
        # Known fail-closed residual: a backtick inside double quotes keeps the
        # span visible (it may execute), so this quoted documentation line still
        # blocks. Narrower than the reported false positive; kept fail-closed.
        "RTK_DISABLED=1 uv run python - <<'PY'\nDOC = \"use `gobby tasks close` here\"\nPY",
    ],
)
def test_blocks_executed_heredoc_and_substitution(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


# Assembled by concatenation so this test module is never itself an invocation
# the guard would match in command position.
_MUTATION = "gobby " + "tasks close 1"


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
def test_find_exec_echo_keeps_arguments_as_data(
    db: HubDatabase, effect: RuleEffect, tool_name: str
) -> None:
    command = f"find . -exec echo -exec {_MUTATION} \\;"
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is False


@pytest.mark.parametrize("prefix", ["echo", "echo -n"])
def test_shell_executes_joined_echo_arguments(
    db: HubDatabase, effect: RuleEffect, prefix: str
) -> None:
    command = f"{prefix} {_MUTATION} | bash"
    assert RuleEngine(db)._should_block(effect, _shell_event("Bash", command)) is True


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "prefix",
    ["--python-preference only-system", "--preview-features audit-command", "--python-fetch never"],
)
@pytest.mark.parametrize("before_run", [True, False])
@pytest.mark.parametrize("blocked", [True, False])
def test_uv_hidden_global_value_options(
    db: HubDatabase,
    effect: RuleEffect,
    tool_name: str,
    prefix: str,
    before_run: bool,
    blocked: bool,
) -> None:
    target = _MUTATION if blocked else "echo " + _MUTATION
    command = f"uv {prefix} run {target}" if before_run else f"uv run {prefix} {target}"
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        (f"echo '{_MUTATION}' | bash", True),
        (f"printf '{_MUTATION}\\n' | sh", True),
        (f"bash <<< '{_MUTATION}'", True),
        (f"find . -exec {_MUTATION} \\;", True),
        (f"find . -exec {_MUTATION} {{}} +", True),
        (f"python -m {_MUTATION}", True),
        (f"uv run python -m {_MUTATION}", True),
        (f"su -c '{_MUTATION}'", True),
        (f"echo '{_MUTATION}'", False),
        (f"printf '{_MUTATION}\\n'", False),
        (f"find . -name '{_MUTATION}'", False),
        (f"python -m echo {_MUTATION}", False),
        (f"X=$(python3 - <<'PY'\nprint('ok')\nPY\n) {_MUTATION}", True),
        (f"X=$({_ODD_PYTHON_SOURCE}) {_MUTATION}", True),
    ],
)
def test_blocks_shell_fed_and_indirect_task_cli(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str, blocked: bool
) -> None:
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    ("body", "blocked"),
    [
        (f"node <<'JS'\n// don't\nJS\n{_MUTATION}", True),
        (_ODD_PYTHON_SOURCE + _MUTATION, True),
        (f"timeout 5 {_MUTATION}", True),
        (f"env A=1 {_MUTATION}", True),
        (f"sudo --login {_MUTATION}", True),
        ('"gobby" tasks close 1', True),
        (f"cat <<'DATA'\n{_MUTATION}\nDATA", False),
    ],
)
def test_executed_heredoc_body_segments_match_separately(
    db: HubDatabase, effect: RuleEffect, tool_name: str, body: str, blocked: bool
) -> None:
    command = "bash <<'EOF'\n" + body + "\nEOF"
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
def test_ruby_variable_named_cat_heredoc_executes(
    db: HubDatabase, effect: RuleEffect, tool_name: str
) -> None:
    command = f"ruby <<'RB'\ncat = <<X\n{_MUTATION}\nX\nsystem(cat)\nRB"
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is True


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
@pytest.mark.parametrize("blocked", [True, False])
def test_xargs_required_operands(
    db: HubDatabase, effect: RuleEffect, tool_name: str, options: str, blocked: bool
) -> None:
    target = _MUTATION if blocked else "echo " + _MUTATION
    event = _shell_event(tool_name, f"xargs {options} {target}")
    assert RuleEngine(db)._should_block(effect, event) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # A `#` comment ends at the newline, so the next line is a real
        # invocation. A comment-unaware masked pass reads the apostrophe as an
        # unterminated single-quote span and blanks that invocation (#23134).
        "# don't\n" + _MUTATION,
        "echo hi # it's\n" + _MUTATION,
        "bash <<'EOF'\n# don't\n" + _MUTATION + "\nEOF",
        "bash <<'EOF'\necho hi  # can't\n" + _MUTATION + "\nEOF",
        "zsh <<'EOF'\n# won't\n" + _MUTATION + "\nEOF",
    ],
)
def test_blocks_command_after_apostrophe_comment(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        # In ANSI-C quoting `\'` is an escaped apostrophe, not the closing
        # quote; misreading it opens a span that blanks the real invocation.
        "echo $'it\\'s'; " + _MUTATION + "; echo 'z'",
        "echo $'it\\'s'\n" + _MUTATION + "\necho 'z'",
        "echo $'x\\'y' | xargs " + _MUTATION + " 'z'",
        "echo \"$(echo $'a\\'b'; " + _MUTATION + ')"',
    ],
)
def test_blocks_command_after_ansi_c_escaped_quote(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo $'it\\'s " + _MUTATION + "'",
        # A decoded newline inside an echo argument is still data.
        "echo $'a\\n" + _MUTATION + "'",
        # bash stops at the unterminated quote the decoded script opens; nothing runs.
        "bash -c $'echo it\\'s; " + _MUTATION + "'",
        # `\c\'` keeps the apostrophe inside the quote, so the rest is echo data.
        "echo $'\\c\\'; " + _MUTATION + " #'",
    ],
)
def test_allows_ansi_c_quoted_data(db: HubDatabase, effect: RuleEffect, command: str) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        'bash -c "' + _MUTATION + '"',
        "bash -c '" + _MUTATION + "'",
        "sh -c 'cd /x && " + _MUTATION + "'",
        'eval "' + _MUTATION + '"',
        "eval '" + _MUTATION + "'",
        "echo 1 | xargs -I{} " + _MUTATION,
        "timeout 5 " + _MUTATION,
        "watch -n1 '" + _MUTATION + "'",
        "ssh host '" + _MUTATION + "'",
        # watch and ssh join every remaining word into the command they run.
        "watch -n1 " + _MUTATION,
        "watch -n 5 -d " + _MUTATION,
        "ssh host " + _MUTATION,
        "ssh -p 22 host " + _MUTATION,
        # `--` ends the shell's options; the script is the word after it.
        "sh -c -- '" + _MUTATION + "'",
        "uv run --with pyyaml " + _MUTATION,
        "uv run --project . --frozen " + _MUTATION,
        # Every value option uv run, ssh and sudo accept consumes its operand,
        # in long, short, attached and clustered forms (#23134).
        "uv run -w pyyaml " + _MUTATION,
        "uv run --link-mode copy " + _MUTATION,
        "uv run --color never " + _MUTATION,
        "uv run -i https://pypi.org/simple " + _MUTATION,
        "uv run -P pyyaml " + _MUTATION,
        "uv run -C key=value " + _MUTATION,
        "uv run --with=pyyaml " + _MUTATION,
        "uv run -wpyyaml " + _MUTATION,
        "uv run -qw pyyaml " + _MUTATION,
        "uv run --quiet -- " + _MUTATION,
        # uv's global options may precede the subcommand.
        "uv -q run " + _MUTATION,
        "uv --directory . run " + _MUTATION,
        "uv --color never --cache-dir /tmp/c run --with pyyaml " + _MUTATION,
        # uvx and `uv tool run` run the named tool's executable.
        "uvx " + _MUTATION,
        "uvx --from gobby " + _MUTATION,
        "uvx -c constraints.txt -w pyyaml " + _MUTATION,
        "uvx --torch-backend cpu " + _MUTATION,
        "uvx gobby@latest tasks close 1",
        "uvx 'gobby[web]' tasks close 1",
        "uvx 'gobby==0.5.0' tasks close 1",
        "uv tool uvx " + _MUTATION,
        "uv tool run " + _MUTATION,
        "uv tool run --from gobby -b build.txt " + _MUTATION,
        "uv -q tool --color never run " + _MUTATION,
        "ssh -E /tmp/ssh.log host " + _MUTATION,
        "ssh -P tag host " + _MUTATION,
        "ssh -O check host " + _MUTATION,
        "ssh -vp 22 host " + _MUTATION,
        "ssh -p22 host " + _MUTATION,
        "ssh -v -- host " + _MUTATION,
        "sudo -D . " + _MUTATION,
        "sudo -nD . " + _MUTATION,
        # env -S splits its string into the command, also at a cluster's end.
        "env -iS '" + _MUTATION + "'",
        "env -S'" + _MUTATION + "'",
        "env -P /usr/bin " + _MUTATION,
        "env -uS " + _MUTATION,
        # bash decodes C escapes inside `$'...'`, so each spells a separator or name.
        "bash -c $'echo hi\\n" + _MUTATION + "'",
        "bash -c $'true;\\t" + _MUTATION + "'",
        "bash -c $'echo hi\\x0a" + _MUTATION + "'",
        "bash -c $'echo hi\\012" + _MUTATION + "'",
        "bash -c $'echo hi\\cJ" + _MUTATION + "'",
        "bash -c $'echo hi\\u000a" + _MUTATION + "'",
        "bash -c $'\\x67" + _MUTATION[1:] + "'",
        # zsh keeps a decoded NUL, so `eval` still runs the command after it.
        "eval $'true\\0; " + _MUTATION + "'",
        "eval $'true\\x00; " + _MUTATION + "'",
        "eval $'true\\c@; " + _MUTATION + "'",
    ],
)
def test_blocks_wrapped_mutating_script(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        # An apostrophe comment no longer blinds the masker; the data payload
        # stays data.
        "uv run python - <<'PY'\n# it's\nDOC = \"\"\"\n" + _MUTATION + '\n"""\nPY',
        "uv run python - <<'PY'\n# don't\nPAYLOAD = '{\"content\": \"" + _MUTATION + "\"}'\nPY",
    ],
)
def test_allows_apostrophe_comment_before_data_payload(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize(
    "command",
    [
        # Wrapper scripts nest; every level must resolve, not just the first.
        "bash -c \"bash -c '" + _MUTATION + "'\"",
        "eval \"bash -c '" + _MUTATION + "'\"",
        "sh -c \"eval '" + _MUTATION + "'\"",
    ],
)
def test_blocks_nested_wrapped_mutating_script(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize("depth", [8, 9, 12])
def test_wrapper_depth_exhaustion_fails_closed(
    db: HubDatabase, effect: RuleEffect, depth: int
) -> None:
    """Wrappers nested past the resolution bound still block (#23134)."""
    command = _MUTATION
    for _ in range(depth):
        command = "sh -c " + shlex.quote(command)

    assert RuleEngine(db)._should_block(effect, _shell_event("Bash", command)) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo " + _MUTATION,
        "uv run --quiet echo " + _MUTATION,
        "uv -q run --with pyyaml echo " + _MUTATION,
        "uvx --from gobby cowsay " + _MUTATION,
        "uv tool run -w pyyaml cowsay " + _MUTATION,
        "uv tool install gobby",
        "env -iS 'echo " + _MUTATION + "'",
        "env -P /usr/bin echo " + _MUTATION,
        "ssh -v host echo " + _MUTATION,
        "ssh -E /tmp/ssh.log host echo " + _MUTATION,
        "sudo -D . echo " + _MUTATION,
    ],
)
def test_allows_mutation_words_as_wrapped_command_data(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    """Resolving a wrapper's options must not turn its command's arguments into code."""
    assert RuleEngine(db)._should_block(effect, _shell_event("Bash", command)) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("env -iS 'echo harmless; " + _MUTATION + "'", False),
        ("env -S 'echo harmless; " + _MUTATION + "'", False),
        ("env -S echo 'harmless; " + _MUTATION + "'", False),
        ("env -S '-i FOO=x echo harmless; " + _MUTATION + "'", False),
        ("env -S 'echo harmless # " + _MUTATION + "'", False),
        ("env -S '-i FOO=x " + _MUTATION + "'", True),
        (r"env -S 'gobby\_tasks\_close\_1'", True),
        ("env -S '\"gobby\" tasks close 1'", True),
        ("env -S '\"gobby tasks close 1\"'", False),
        (r"env -S 'gobby\ntasks close 1'", False),
        (r"env -S 'echo harmless\c; " + _MUTATION + "'", False),
        (r"env -S '\"echo\_harmless\" " + _MUTATION + "'", False),
        ("env -S '' " + _MUTATION, True),
        ("env -S '-S \"" + _MUTATION + "\"'", True),
        ("env -S '" + _MUTATION + " \"'", True),
        ("env --split '" + _MUTATION + "'", True),
        ("env --spl='" + _MUTATION + "'", True),
        ("env --env0-from /tmp/vars " + _MUTATION, True),
        ("env --quoting-style shell " + _MUTATION, True),
        ("env --chd /tmp " + _MUTATION, True),
        ("sudo --chd /tmp " + _MUTATION, True),
        ("sudo --us root " + _MUTATION, True),
        ("sudo -a auth " + _MUTATION, True),
        ("sudo -c staff " + _MUTATION, True),
        ("sudo -r role " + _MUTATION, True),
        ("sudo -t type " + _MUTATION, True),
        ("sudo --auth-type auth " + _MUTATION, True),
        ("sudo --login-class staff " + _MUTATION, True),
        ("sudo --role role " + _MUTATION, True),
        ("sudo --type type " + _MUTATION, True),
        ("watch --inter 5 " + _MUTATION, True),
        ("uv run --no-build " + _MUTATION, True),
        ("uvx --no-build " + _MUTATION, True),
    ],
)
def test_env_split_and_getopt_prefixes_preserve_execution(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str, blocked: bool
) -> None:
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize("prefix", ["sudo --login", "sudo --login-class x", "sudo --login-c x"])
def test_sudo_exact_flag_precedes_value_prefix(
    db: HubDatabase, effect: RuleEffect, tool_name: str, prefix: str
) -> None:
    command = prefix + " " + _MUTATION
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is True


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize("data", ["it's", 'a "quote', "`literal", "plain"])
@pytest.mark.parametrize("consumer,blocked", [("bash", True), ("cat", False)])
def test_nested_heredoc_data_does_not_open_shell_quote(
    db: HubDatabase,
    effect: RuleEffect,
    tool_name: str,
    data: str,
    consumer: str,
    blocked: bool,
) -> None:
    command = consumer + " <<'EOF'\ncat <<'X'\n" + data + "\nX\n" + _MUTATION + "\nEOF"
    assert RuleEngine(db)._should_block(effect, _shell_event(tool_name, command)) is blocked


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # Quotes and backslashes inside a word vanish before it runs, so each of
        # these runs the mutation (#23134).
        "'gobby'" + _MUTATION[5:],
        '"gobby"' + _MUTATION[5:],
        "go''bby" + _MUTATION[5:],
        "g\\obby" + _MUTATION[5:],
        "\\gobby" + _MUTATION[5:],
        "gobby 'tasks'" + _MUTATION[11:],
        "gobby t\\asks" + _MUTATION[11:],
        "sudo 'gobby'" + _MUTATION[5:],
        "$'\\x67obby'" + _MUTATION[5:],
        "eval '\\gobby" + _MUTATION[5:] + "'",
        # zsh drops the backslash of an unknown `$'...'` escape.
        "$'\\gobby'" + _MUTATION[5:],
        "eval $'\\gobby" + _MUTATION[5:] + "'",
        # zsh reads `\C-j` as a newline, which separates the commands.
        "eval $'true\\C-j" + _MUTATION + "'",
    ],
)
def test_blocks_quoted_or_escaped_command_word(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        # Decoded words are re-quoted before matching, so a word holding a
        # separator or newline stays one data argument.
        "echo 'x; " + _MUTATION + "'",
        "git commit -m $'subject\\n" + _MUTATION + "'",
        # A dequoted name in argument position is still only an argument.
        "echo 'gobby'" + _MUTATION[5:],
    ],
)
def test_allows_dequoted_words_in_data_position(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize(
    "command",
    [
        # An unquoted word-initial `#` starts a comment, so its apostrophe opens
        # no quote and the quoted command word is still dequoted (#23134 R4 F1).
        "'gob'by" + _MUTATION[5:] + " # it's",
        "true # it's\n'gob'by" + _MUTATION[5:],
        "g\\obby" + _MUTATION[5:] + " # don't",
        # A `#` inside a word or a quoted word starts no comment.
        "echo 'a # b' && 'gob'by" + _MUTATION[5:],
        "echo a#'b' && 'gob'by" + _MUTATION[5:],
        # Inside backticks the closing backtick also ends the comment.
        "echo `true # x` ; " + _MUTATION,
        "x=`echo # it's` ; 'gob'by" + _MUTATION[5:],
    ],
)
def test_blocks_quoted_command_word_beside_apostrophe_comment(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        "true # it's fine\necho '" + _MUTATION + "'",
        "echo '" + _MUTATION + "' # don't run it",
    ],
)
def test_allows_quoted_data_beside_apostrophe_comment(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is False
