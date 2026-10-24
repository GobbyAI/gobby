"""Approving unowned dirty paths is the operator's decision; no seat may run the approval."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-dirty-path-approval-cli"
EXPANSION_RULE = "block-gobby-cli-expansion"
BOUNDED_RULE = "block-gobby-command-word-expansion"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
# Every launcher, wrapper and global-option spelling that reaches the CLI.
INVOCATIONS = (
    'gobby sessions {approve-dirty-path"s",s7} a.txt',
    "gobby sessions approve-dirty-paths s7 --checkout /repo a.txt",
    "cd /repo && uv run gobby sessions approve-dirty-paths s7 a.txt",
    "env gobby sessions approve-dirty-paths s7 --checkout /repo a.txt",
    "env -i PATH=/usr/bin gobby sessions approve-dirty-paths s7 a.txt",
    "uv run --directory /repo gobby sessions approve-dirty-paths s7 a.txt",
    "uv run --no-sync gobby sessions approve-dirty-paths s7 a.txt",
    "uvx gobby sessions approve-dirty-paths s7 a.txt",
    "python -m gobby.cli sessions approve-dirty-paths s7 a.txt",
    "gobby --verbose sessions approve-dirty-paths s7 a.txt",
    ".venv/bin/gobby sessions approve-dirty-paths s7 a.txt",
    "g=gobby; $g sessions approve-dirty-paths s7 a.txt",
    'gobby sessions approve-dirty-path"s" s7 a.txt',
    "bash -c 'gobby sessions approve-dirty-paths s7 a.txt'",
    'echo "$(gobby sessions approve-dirty-paths s7 a.txt)"',
    "a=approve-dirty-; b=paths; gobby sessions $a$b s7 --checkout /repo a.txt",
    "a=approve-dirty-; b=paths; gobby 'sessions' $a$b s7 a.txt",
    'a=approve-dirty-; b=paths; gobby "sessions" $a$b s7 a.txt',
    "a=approve-dirty-; b=paths; gobby se\\ssions $a$b s7 a.txt",
    "a=approve-dirty-; b=paths; gobby sess'ions' $a$b s7 a.txt",
    'a=approve-dirty-; b=paths; gobby sessions "$a$b" s7 a.txt',
    'eval "gobby sessions approve-dirty-paths s7 a.txt"',
    "sh -c 'gobby sessions $v s7 a.txt'",
    'gobby sessions list --project "$(gobby sessions approve-dirty-paths s7 a.txt)"',
    "gobby sessions list --project $P; a=approve-dirty-; b=paths; $g sessions $a$b s7 a.txt",
)
# Ordinary arguments, other gobby subcommands and literal globs stay allowed.
# Protected brace spellings are covered separately below.
ALLOWED = (
    'gobby sessions show "$SID"',
    "gobby sessions approve-dirty-p?ths s7 a.txt",
    "gobby sessions approve-dirty-p[a]ths s7 a.txt",
    "gobby tasks show $TASK",
    "gobby tasks show {a,b}",
    "gobby tasks list --status *.md",
    """gobby tasks create --data '{"title": "sessions $HOME"}'""",
    "gcode search-content 'approve-dirty-paths'",
)


def _bash_event(command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=GOBBY_PROJECT_ID,
        data={"command": command, "tool_input": {"command": command}, "tool_name": "Bash"},
    )


@pytest.mark.parametrize(
    "variables",
    [{"is_spawned_agent": True}, {}],
    ids=["spawned", "interactive"],
)
async def test_dirty_path_approval_rules_match_criterion_4_scope(
    temp_db: HubDatabase, variables: dict[str, object]
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute(
        "DELETE FROM rule_definitions WHERE name NOT IN (%s, %s, %s)",
        (RULE_NAME, EXPANSION_RULE, BOUNDED_RULE),
    )
    engine = RuleEngine(temp_db)

    async def decide(command: str) -> tuple[str, str]:
        response = await engine.evaluate(
            _bash_event(command), session_id=SESSION_ID, variables=dict(variables)
        )
        return response.decision, response.reason or ""

    direct = await decide("gobby sessions approve-dirty-paths s7 --checkout /repo a.txt")
    decisions = {
        command: (await decide(command))[0]
        for command in (
            *INVOCATIONS,
            *ALLOWED,
            'echo "ask the operator: gobby sessions approve-dirty-paths s7 a.txt"',
            "git commit -m 'relay gobby sessions approve-dirty-paths' -- a.txt",
            "gobby sessions list",
            "gobby sessions list --project $PROJECT",
            "uv run gobby sessions list --project $PROJECT",
        )
    }

    assert "operator" in direct[1]
    assert decisions == {
        **dict.fromkeys(INVOCATIONS, "block"),
        **dict.fromkeys(ALLOWED, "allow"),
        'echo "ask the operator: gobby sessions approve-dirty-paths s7 a.txt"': "allow",
        "git commit -m 'relay gobby sessions approve-dirty-paths' -- a.txt": "allow",
        "gobby sessions list": "allow",
        "gobby sessions list --project $PROJECT": "allow",
        "uv run gobby sessions list --project $PROJECT": "allow",
    }


BRACE_REFUSED = (
    'gobby sessions {approve-dirty-path"s",s7} a.txt',
    "gobby {tasks,sessions} list",
    "gobby tasks {show,list}",
    "gobby --config /tmp/x tasks {list,}",
    "gobby --config=/tmp/{x,y} tasks list",
    "gobby --config {x,sessions} approve-dirty-paths s7",
    "gobby 'tasks'{,} list",
    "gobby tasks} list",
    "gobby tasks show{ x",
    "gobby{ tasks list",
    "{gobby tasks list",
    "gobby {tasks list",
    "gobby sessions --verbose {approve-dirty-paths,} s7",
    "gobby sessions $SUB s7",
    'gobby "sessions" "$SUB" s7',
    "sudo -u root gobby sessions approve-dirty-paths s7",
    "doas -u root gobby sessions approve-dirty-paths s7",
    "caffeinate g{o}bby sessions a{p}prove-dirty-paths s7",
    "/usr/bin/arch -arch arm64 {gobby,} sessions list",
    "unknown g{o..o}bby sessions list",
    "unknown {f..h}{n..p}{a..c}{a..c}{x..z} sessions list",
    "unknown gobby sessions approve-{dirty-paths,}",
    "sh -c 'gobby tasks {list,}'",
    "eval 'gobby tasks {list,}'",
    "find . -exec gobby tasks {list,} \\;",
    "echo $(gobby tasks {list,})",
    """uv run python - <<PY
print("$(gobby sessions {approve,list}-dirty-paths s7)")
PY""",
    """docker exec -i c sh <<'EOF'
gobby sessions {approve,list}-dirty-paths s7
EOF""",
    """uv run bash -s python <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    """python3 - <<'PY'
print(1)
PY
gobby sessions {approve,list}-dirty-paths s7""",
    """(uv run python - <<PY
print("$(gobby sessions {approve,list}-dirty-paths s7)")
PY
)""",
    """(bash <<'E'
gobby sessions {approve,list}-dirty-paths s7
E
)""",
    'for d in a b; do echo "$d $(gobby sessions {approve,list}-dirty-paths s7)"; done',
    """python3 -c 'import os,sys;os.system(sys.stdin.read())' <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    """uv run python -c 'import os,sys;os.system(sys.stdin.read())' <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    """env python3 -c 'import os,sys;os.system(sys.stdin.read())' <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    """python3 -Ic 'import os,sys;os.system(sys.stdin.read())' <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    """python3 -c 'import os,sys;os.system(sys.stdin.read())' <<E
gobby sessions {approve,list}-dirty-paths s7
E""",
    """(python3 -c 'import os,sys;os.system(sys.stdin.read())' <<'E'
gobby sessions {approve,list}-dirty-paths s7
E
)""",
    """uv run python -m runner <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    """python3 run.py <<'E'
gobby sessions {approve,list}-dirty-paths s7
E""",
    "C=/x/gobby; git bisect run a..$C",
    "git grep --open-files-in-pager {gobby,x} pattern",
    "git log --oneline {gobby,x}",
)


async def test_unquoted_brace_in_gobby_command_words_is_refused(
    temp_db: HubDatabase,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute(
        "DELETE FROM rule_definitions WHERE name NOT IN (%s, %s, %s)",
        (RULE_NAME, EXPANSION_RULE, BOUNDED_RULE),
    )
    engine = RuleEngine(temp_db)
    decisions = {}
    for command in BRACE_REFUSED:
        response = await engine.evaluate(_bash_event(command), session_id=SESSION_ID, variables={})
        decisions[command] = response.decision
    assert decisions == dict.fromkeys(BRACE_REFUSED, "block")


BRACE_ALLOWED = (
    "gobby tasks show {a,b}",
    "gobby sessions list --project {a,b}",
    'gobby tasks "{show,list}"',
    r"gobby \{tasks,x\} list",
    'gobby --config "{a,b}" tasks list',
    'gobby --config "$X" tasks list',
    'gobby --config="$X" tasks list',
    'rg -n example src/gobby/sessions "$P"',
    "gcode grep -w sessions src/gobby/{cli,servers} -m 50",
    "rg sessions -g *.{py,md}",
    "rg -n gobby src/gobby/{sessions,tasks}",
    'gobby tasks show "$ID"',
    "uv run --project /tmp pytest tests/{a,b}.py",
    "echo {a,b}",
    "ls ${HOME}",
    'echo "gobby {tasks,x} list"',
    "git commit -m 'gobby {tasks,} list'",
    """uv run python - <<'PY'
import subprocess
queries = [{"op": "grep", "pattern": "resolve_operator_conversation"}]
for q in queries:
    subprocess.run(["gcode", "grep", "-w", q["pattern"], "-m", "50", "--project", "/repo"])
PY""",
    """uv run python -c 'import subprocess; subprocess.run(["gcode", "grep", "-w", "x", "-m", "50"])'""",
    """(uv run python - <<'PY'
queries = [{"op": "grep", "pattern": "resolve_operator_conversation"}]
PY
)""",
    r"""for d in task-23678-dispatch-batch-retirement task-24244-decision-service; do echo "$d $(lsof -t +D /Users/josh/.gobby/worktrees/gobby/$d 2>/dev/null | tr '\n' ' ')"; done""",
    """python3 <<'PY'
print({"op": "grep"})
PY""",
    """python3 - <<'PY'
queries = [{"op": "grep", "pattern": "x"}]
PY""",
    "C=2894b82caa4be295eae44877b08f302d2d381afb; git log --oneline 0.5.0..$C",
    "git log a..$C",
)


async def test_literal_command_slots_and_brace_arguments_stay_allowed(
    temp_db: HubDatabase,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute(
        "DELETE FROM rule_definitions WHERE name NOT IN (%s, %s, %s)",
        (RULE_NAME, EXPANSION_RULE, BOUNDED_RULE),
    )
    engine = RuleEngine(temp_db)
    decisions = {}
    for command in BRACE_ALLOWED:
        response = await engine.evaluate(_bash_event(command), session_id=SESSION_ID, variables={})
        decisions[command] = response.decision
    assert decisions == dict.fromkeys(BRACE_ALLOWED, "allow")


def test_long_python_and_brace_commands_finish_outside_the_daemon() -> None:
    # A process timeout can stop a regex holding the GIL; pytest's thread-based
    # timeout cannot. Exercise every bundled selector and condition together.
    program = """
import yaml
from gobby.workflows.engine.command_matching import command_patterns_match
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_path

path = get_bundled_rules_path() / 'task-enforcement/block-dirty-path-approval-cli.yaml'
rules = yaml.safe_load(path.read_text())['rules']
helpers = build_condition_helpers()
commands = [
    "uv run python -c '" + "value = str(123); " * 1500 + "'",
    "uv run python -c '" + "x" * 30000 + "'",
    "uv run python -c '" + "{a,b}" * 6000 + "'",
    "echo " + "{" * 15000,
    "echo " + "{a,b}" * 15000,
]
for command in commands:
    for rule in rules.values():
        context = {'tool_input': {'command': command}}
        evaluator = SafeExpressionEvaluator(context, allowed_funcs=helpers)
        condition = not rule.get('when') or evaluator.evaluate(rule['when'])
        for effect in rule['effects']:
            matches = command_patterns_match(
                command, pattern=effect.get('command_pattern'),
                mask_quoted=effect.get('mask_quoted', False),
            )
            assert not (condition and matches), command[:80]
command = commands[0] + '; gobby tasks {list,}'
blocked = False
for rule in rules.values():
    context = {'tool_input': {'command': command}}
    evaluator = SafeExpressionEvaluator(context, allowed_funcs=helpers)
    condition = not rule.get('when') or evaluator.evaluate(rule['when'])
    for effect in rule['effects']:
        blocked |= bool(condition and command_patterns_match(
            command, pattern=effect.get('command_pattern'),
            mask_quoted=effect.get('mask_quoted', False),
        ))
assert blocked, 'protected command after long Python data was missed'
print('bounded selectors pass')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded selectors pass"


def test_long_python_data_with_many_quotes_and_braces_stays_literal() -> None:
    # Additional proof requested after the initial correction: no behavior
    # change, and the retained RED of the protected-suffix test stays original.
    program = """
from gobby.workflows.approval_brace_guard import approval_command_word_expansion
command = "uv run python -c '" + 'value = {"key": "value"}; ' * 2000 + "'"
assert not approval_command_word_expansion(command)
print('quoted Python data stays literal')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "quoted Python data stays literal"


def test_command_word_expansion_pattern_is_bounded_on_long_uv_run_probe() -> None:
    # The rule's `when` is the bounded token scanner in approval_brace_guard.py,
    # not a regex. The probe repeats the `uv run --directory` phrase that pinned the
    # daemon's rule loop. A child process keeps a regressed scanner from holding
    # pytest's GIL past the timeout.
    probe = "uv run --no-sync python -c '" + "uv run --directory /repo " * 95 + "'"
    assert len(probe) == 2404
    program = """
import sys
import time

import yaml

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_path

path = get_bundled_rules_path() / 'task-enforcement/block-dirty-path-approval-cli.yaml'
rule = yaml.safe_load(path.read_text())['rules']['block-gobby-command-word-expansion']
helpers = build_condition_helpers()


def blocked(command: str) -> bool:
    evaluator = SafeExpressionEvaluator({'tool_input': {'command': command}}, allowed_funcs=helpers)
    return bool(evaluator.evaluate(rule['when']))


started = time.perf_counter()
assert not blocked(sys.argv[1])
assert blocked('gobby sessions {list,} ' + sys.argv[1])
assert time.perf_counter() - started < 1.0
print('bounded word expansion passes')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, probe],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded word expansion passes"
