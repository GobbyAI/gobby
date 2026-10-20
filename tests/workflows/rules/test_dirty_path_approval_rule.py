"""Approving unowned dirty paths is the operator's decision; no seat may run the approval."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-dirty-path-approval-cli"
RULE_FILE = get_bundled_rules_path() / "task-enforcement" / f"{RULE_NAME}.yaml"
CLI_SOURCE = Path(__file__).resolve().parents[3] / "src" / "gobby" / "cli" / "__init__.py"
EXPANSION_RULE = "block-gobby-cli-expansion"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
# Every launcher, wrapper and global-option spelling that reaches the CLI.
INVOCATIONS = (
    "sudo -u root gobby sessions approve-dirty-paths s7 a.txt",
    "sudo -i gobby sessions approve-dirty-paths s7",
    "doas -u root gobby sessions approve-dirty-paths s7 a.txt",
    "sh -c 'gobby sessions approve-dirty-paths s7 a.txt'",
    'bash -c "gobby sessions approve-dirty-paths s7 a.txt"',
    "zsh -c 'gobby sessions approve-dirty-paths s7'",
    "eval 'gobby sessions approve-dirty-paths s7'",
    'sh -c "gobby sessions approve-dirty-paths s7',
    "eval 'gobby sessions approve-dirty-\"paths\" s7'",
    "xargs sh -c 'gobby sessions approve-dirty-paths s7'",
    "xargs gobby sessions approve-dirty-paths s7 a.txt",
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
    'gobby sessions {approve-dirty-path"s",s7} a.txt',
    "gobby sessions approve-dirty-{paths,x} s7 a.txt",
    "gobby sessions {approve-dirty-paths,} s7 a.txt",
    "gobby --verbose sessions {approve-dirty-paths,x} s7 a.txt",
    "gobby sessions {{approve-dirty-paths,x},y} s7 a.txt",
    "gobby sessions {approve-dirty-paths,{x,y}} s7 a.txt",
    "gobby sessions {{approve-dirty-paths,x},} s7 a.txt",
    "uv run gobby sessions approve-dirty-{{paths,x},y} s7 a.txt",
    "gobby {sessions,approve-dirty-paths} s7 a.txt",
    "{gobby,sessions,approve-dirty-paths} s7 a.txt",
    "uv run gobby --verbose {sessions,approve-dirty-paths} s7 --checkout /repo a.txt",
    "{{gobby,x},sessions} approve-dirty-paths s7 a.txt",
    ".venv/bin/gobby {sessions,} approve-dirty-paths s7 --checkout . a.txt",
    "~/.local/bin/gobby {sessions,} approve-dirty-paths s7 --checkout . a.txt",
    '"gobby" {sessions,} approve-dirty-paths s7 --checkout . a.txt',
    "g\\obby {sessions,} approve-dirty-paths s7 --checkout . a.txt",
    'gobby {"sessions",} {"approve-dirty-paths",} s7 --checkout . a.txt',
    "gobby {sess\\ions,} {appr\\ove-dirty-paths,} s7 --checkout . a.txt",
    "gobby session{s..s} approve-dirty-path{s..s} s7 --checkout . a.txt",
    "gobby {{sessions,},} approve-dirty-paths s7 --checkout . a.txt",
    "{bash,} -c 'gobby sessions approve-dirty-paths s7 --checkout . a.txt'",
    "gobby sessions --verbose {approve-dirty-paths,} s7 a.txt",
    "gobby {sessions,} {approve-dirty-paths,} s7 --checkout . {'..'}",
    "gobby {sessions,} {approve-dirty-paths,} s7 --checkout . {\\..a}",
    "gobby session{\\s..\\s} approve-dirty-path{\\s..\\s} s7 --checkout . a.txt",
    "gobby session{'s'..'s'} approve-dirty-path{'s'..'s'} s7 --checkout . a.txt",
    "gobby sessions approve{-..-}dirty{-..-}paths s7 --checkout . a.txt",
    "gobby s{essions,} approve-{dirty,x}-paths s7 a.txt",
    "gobby s{essions,} approve-dirty-paths s7 a.txt",
    "gobby {s,}essions approve-dirty-paths s7 a.txt",
    "gobby sessions {x,approve-dirty-paths} s7 a.txt",
)
# Globs, quoted arguments, other gobby subcommands, and braces outside the command,
# group and subcommand words. Quoted and escaped braces are literal, so they pass.
ALLOWED = (
    'gobby sessions show "$SID"',
    "gobby sessions list --project {a,b}",
    "find . -name sessions -exec cat {} \\;",
    "gobby sessions approve-dirty-p?ths s7 a.txt",
    "gobby sessions approve-dirty-p[a]ths s7 a.txt",
    "gobby tasks show $TASK",
    "gobby tasks show {a,b}",
    "gobby tasks list --status *.md",
    """gobby tasks create --data '{"title": "sessions $HOME"}'""",
    "gcode search-content 'approve-dirty-paths'",
    "ls src/gobby/{sessions,tasks}",
    "gcode outline src/gobby/{sessions,tasks}/manager.py",
    "wc -l src/gobby/servers/routes/{sessions,tasks}/*.py",
    "git diff 0.5.0 -- src/gobby/{sessions,storage}/",
    "uv run pytest tests/{sessions,workflows}/test_x.py -q",
    "cat docs/{guides,reference}/sessions.md",
    "rg -g *.{py,md} sessions src",
    "grep -rn sessions src/gobby/{cli,servers}",
    "rg sessions -g *.{py,md}",
    "gcode grep -w sessions src/gobby/{cli,servers} -m 50",
    "rg -n gobby src/gobby/{sessions,tasks}",
    "git -C /repo/gobby log -- src/gobby/{sessions,tasks}",
    '.venv/bin/gobby "{sessions,}" approve-dirty-paths s7 a.txt',
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
        "DELETE FROM rule_definitions WHERE name NOT IN (%s, %s)", (RULE_NAME, EXPANSION_RULE)
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


# A brace in a gobby command's group, subcommand or global option value is refused, flags and
# wrappers aside: it can expand to the approval subcommand. A brace inside a gobby command word,
# including a one-letter range such as g{o..o}bby, is refused too. Any unquoted brace, `$` or
# backtick in the executable word is refused whatever letters surround it, because no real
# command needs a non-literal executable word. Quoted and escaped spellings are literal, and
# braces in later arguments pass.
BRACE_REFUSED = (
    "find . -exec g{o..o}bby tasks list \\;",
    "find . -name x -exec gobby sessions {approve-dirty-paths,} s7 \\;",
    "find . -execdir $X tasks list \\;",
    "command g{o..o}bby sessions {approve-dirty-paths,} s7 a.txt",
    "command -v g{o..o}bby tasks list",
    "command -p $X tasks list",
    "exec -a x g{o..o}bby sessions {approve-dirty-paths,} s7",
    "exec -a $X gobby tasks list",
    "exec -c g{o..o}bby tasks list",
    "exec g{o..o}bby tasks list",
    "nice -n 5 g{o..o}bby sessions {approve-dirty-paths,} s7",
    "nice --adjustment=5 g{o..o}bby tasks list",
    "nice --adjustment 5 g{o..o}bby tasks list",
    "nice -n $N gobby tasks list",
    "nice -5 g{o..o}bby tasks list",
    "nice g{o..o}bby sessions {approve-dirty-paths,} s7",
    "timeout 5 g{o..o}bby sessions {approve-dirty-paths,} s7",
    "timeout -s TERM 5 g{o..o}bby tasks list",
    "timeout -k 1 -s TERM 5 g{o..o}bby tasks list",
    "timeout --signal=TERM 5 g{o..o}bby tasks list",
    "timeout --kill-after 1 5 g{o..o}bby tasks list",
    "timeout 5s g{o..o}bby tasks list",
    "timeout $T gobby tasks list",
    "timeout -s $SIG 5 gobby tasks list",
    "timeout 5 $X tasks list",
    "nohup timeout 5 g{o..o}bby tasks list",
    "xargs g{o..o}bby sessions {approve-dirty-paths,} s7",
    "xargs -I x g{o..o}bby sessions {approve-dirty-paths,} s7",
    "xargs -n 1 g{o..o}bby tasks list",
    "xargs -0 -P 4 g{o..o}bby tasks list",
    "xargs -E x g{o..o}bby tasks list",
    "xargs -s 100 g{o..o}bby tasks list",
    "xargs --max-args=1 g{o..o}bby tasks list",
    "xargs -a f g{o..o}bby tasks list",
    "xargs -I $X gobby tasks list",
    "xargs -n $N gobby tasks list",
    "xargs --max-procs $N gobby tasks list",
    "xargs -I {x,y} gobby tasks list",
    "xargs $X sessions approve-dirty-paths",
    "time xargs g{o..o}bby tasks list",
    "{gobby,tasks} list",
    "{bash,} -c 'gobby tasks list'",
    "gobby {tasks,sessions} list",
    "gobby {{tasks,x},y} list",
    "gobby ta{s..s}ks list",
    "gobby {tasks list",
    "gobby tasks} list",
    "gobby tasks {show,list} x",
    "gobby tasks {{show,x},y} x",
    "gobby tasks show{ x",
    ".venv/bin/gobby {tasks,} list",
    "~/.local/bin/gobby tasks {list,}",
    '"gobby" {tasks,} list',
    "g\\obby {tasks,} list",
    "gobby 'tasks'{,} list",
    "uv run gobby --verbose {tasks,} list",
    "uv run --directory /repo gobby {tasks,} list",
    "env -i PATH=/usr/bin gobby {tasks,} list",
    "gobby --verbose tasks {list,}",
    "gobby tasks --verbose {list,}",
    "gobby --config /tmp/x sessions {approve-dirty-paths,} s7 a.txt",
    "gobby --config=/tmp/x sessions {approve-dirty-paths,} s7 a.txt",
    'gobby --config "/tmp/my dir/x" sessions {approve-dirty-paths,} s7 a.txt',
    "gobby --config /tmp/x tasks {list,}",
    "gobby --config {x,sessions} approve-dirty-paths s7 a.txt",
    "gobby --config=/tmp/{x,y} tasks list",
    "gobby --config /tmp/x {tasks,} list",
    "gobb{y..y} sessions {approve-dirty-paths,} s7 a.txt",
    "gobb{y..y} tasks list",
    "uv run {gobby,} sessions {approve-dirty-paths,} s7 a.txt",
    "uv run {gobby,} tasks list",
    "gobby{ tasks list",
    "gobby} tasks list",
    "{gobby tasks list",
    "g{obby,} tasks list",
    "{g..g}obby tasks list",
    "g{o..o}bby tasks list",
    "go{b..b}by tasks list",
    "gob{b..b}y tasks list",
    "{g..g}obby sessions {approve-dirty-paths,} s7 a.txt",
    "g{o..o}bby sessions {approve-dirty-paths,} s7 a.txt",
    "go{b..b}by sessions {approve-dirty-paths,} s7 a.txt",
    "gob{b..b}y sessions {approve-dirty-paths,} s7 a.txt",
    ".venv/bin/g{o..o}bby tasks list",
    "uv run g{o..o}bby sessions {approve-dirty-paths,} s7 a.txt",
    "uv run --directory /repo {g..g}obby tasks list",
    "env -i PATH=/usr/bin go{b..b}by tasks list",
    "$x tasks list",
    "${x}by tasks list",
    "go${x}by tasks list",
    "g`x`obby tasks list",
    "gobby`x` tasks list",
    "{bash,} -c 'echo hi'",
    "{gobby,x} tasks list",
    "$HOME/bin/tool a.txt",
    "{ gobby sessions {approve-dirty-paths,} s7 a.txt; }",
    "{ gobby tasks {list,}; }",
    "X=1 g{o..o}bby sessions {approve-dirty-paths,} s7 a.txt",
    "X=1 gobby sessions {approve-dirty-paths,} s7 a.txt",
    "X=1 gobby tasks {list,}",
    "X=1 $x tasks list",
    "X=1 uv run g{o..o}bby tasks list",
    "X=1 env -i g{o..o}bby tasks list",
    "X=1 Y=2 Z=3 g{o..o}bby tasks list",
    "X='a b' Y=\"c d\" g{o..o}bby tasks list",
    "X=\"1\" Y='2' gobby sessions {approve-dirty-paths,} s7 a.txt",
    "X=a\\ b g{o..o}bby tasks list",
    "{ X=1 gobby sessions {approve-dirty-paths,} s7 a.txt; }",
    "env X=1 uv run g{o..o}bby tasks list",
    "env X=1 Y=2 g{o..o}bby tasks list",
    "env -i X=1 g{o..o}bby tasks list",
    "env -u HOME g{o..o}bby tasks list",
    "env -iu HOME g{o..o}bby tasks list",
    "env -uHOME g{o..o}bby tasks list",
    "env -C /tmp g{o..o}bby tasks list",
    "env -S 'x' g{o..o}bby tasks list",
    "uv run --project /tmp g{o..o}bby tasks list",
    "uv run --project=/tmp g{o..o}bby tasks list",
    "uv run --directory /repo g{o..o}bby tasks list",
    "uv run --directory=/repo g{o..o}bby tasks list",
    "uv run --python 3.13 g{o..o}bby tasks list",
    "uv run --python=3.13 g{o..o}bby tasks list",
    "uv run -p 3.13 g{o..o}bby tasks list",
    "uv run -p=3.13 g{o..o}bby tasks list",
    "uv run -p3.13 g{o..o}bby tasks list",
    "uv run --with pytest g{o..o}bby tasks list",
    "uv run --with=pytest g{o..o}bby tasks list",
    "uv run -w pytest g{o..o}bby tasks list",
    "uv run --no-sync --project /tmp g{o..o}bby tasks list",
    "uv run --project /tmp uv run g{o..o}bby tasks list",
    "uv run --project /tmp gobby sessions {approve-dirty-paths,} s7 a.txt",
    "uv run --project /tmp gobby tasks {list,}",
    "uv run --project $P tasks list",
    "uv run --directory $P sessions {approve-dirty-paths,} s7 a.txt",
    "uv run --project {x,gobby} sessions {approve-dirty-paths,} s7 a.txt",
    "env X=$P gobby tasks list",
    "env -u $X gobby tasks list",
    '"$x" sessions {approve-dirty-paths,} s7 a.txt',
    '"$x" tasks list',
    '"$x"tasks list',
    'gobby "$x" approve-dirty-paths s7 a.txt',
    'gobby "$x" sessions approve-dirty-paths s7 a.txt',
    'gobby "$x" tasks list',
    'gobby "$x" "$y" s7 a.txt',
    "gobby $x approve-dirty-paths s7 a.txt",
    "gobby $x tasks list",
    'gobby tasks "$x"',
    "gobby --config $x tasks list",
    "gobby --config=$x tasks list",
    "uvx --from x g{o..o}bby tasks list",
    "uvx --from=x g{o..o}bby tasks list",
    "uvx --with pytest g{o..o}bby tasks list",
    "uvx --python 3.13 g{o..o}bby tasks list",
    "uvx -p 3.13 g{o..o}bby tasks list",
    "uvx -qp 3.13 g{o..o}bby tasks list",
    "uvx --constraints c.txt g{o..o}bby tasks list",
    "uvx --from x gobby sessions {approve-dirty-paths,} s7 a.txt",
    "uvx gobby sessions {approve-dirty-paths,} s7 a.txt",
    "uvx --from $X gobby tasks list",
    "uv tool run --from x g{o..o}bby tasks list",
    "nohup g{o..o}bby tasks list",
    "nohup gobby sessions {approve-dirty-paths,} s7 a.txt",
    "time g{o..o}bby tasks list",
    "time -p g{o..o}bby tasks list",
    "time -o /tmp/t g{o..o}bby tasks list",
    "time -o=/tmp/t g{o..o}bby tasks list",
    "time -o/tmp/t g{o..o}bby tasks list",
    "time -o $F gobby tasks list",
    "nohup time -p g{o..o}bby tasks list",
    "nohup uv run --project /tmp g{o..o}bby tasks list",
    "time uv run --project /tmp g{o..o}bby tasks list",
    "sudo g{o..o}bby tasks list",
    "sudo -u root gobby sessions {approve-dirty-paths,} s7",
    "sudo -u $X gobby tasks list",
    "sudo -u {a,b} gobby tasks list",
    "sudo -uroot g{o..o}bby tasks list",
    "sudo --user=root g{o..o}bby tasks list",
    "sudo --user root g{o..o}bby tasks list",
    "sudo -iu root g{o..o}bby tasks list",
    "sudo -E g{o..o}bby tasks list",
    "sudo -- g{o..o}bby tasks list",
    "sudo -n -u root g{o..o}bby tasks list",
    "sudo -C 3 g{o..o}bby tasks list",
    "sudo -D /tmp g{o..o}bby tasks list",
    "sudo -h host g{o..o}bby tasks list",
    "sudo -p prompt g{o..o}bby tasks list",
    "sudo -T 5 g{o..o}bby tasks list",
    "sudo -U root g{o..o}bby tasks list",
    "sudo -g wheel g{o..o}bby tasks list",
    "sudo -R /r g{o..o}bby tasks list",
    "sudo --close-from=3 g{o..o}bby tasks list",
    "sudo --chdir=/tmp g{o..o}bby tasks list",
    "sudo --preserve-env=PATH g{o..o}bby tasks list",
    "sudo --preserve-env g{o..o}bby tasks list",
    "sudo -r role g{o..o}bby tasks list",
    "sudo -i 'g{o..o}bby tasks list'",
    "sudo -s 'gobby sessions {approve-dirty-paths,} s7'",
    "sudo -i gobby sessions {approve-dirty-paths,} s7",
    "sudo -i 'echo $HOME'",
    "sudo -u $X -i gobby tasks list",
    "sudo -u root uv run g{o..o}bby tasks list",
    "sudo nice -n 5 g{o..o}bby tasks list",
    "sudo timeout 5 g{o..o}bby tasks list",
    "sudo env X=1 g{o..o}bby tasks list",
    "nice sudo -u root g{o..o}bby tasks list",
    "nice sudo -Z gobby tasks list",
    "find . -exec sudo g{o..o}bby tasks list \\;",
    "find . -exec sudo -Z gobby tasks list \\;",
    "doas g{o..o}bby tasks list",
    "doas -u root gobby sessions {approve-dirty-paths,} s7",
    "doas -u $X gobby tasks list",
    "doas -C /etc/doas.conf g{o..o}bby tasks list",
    "doas -a style g{o..o}bby tasks list",
    "doas -nu root g{o..o}bby tasks list",
    "doas -L g{o..o}bby tasks list",
    "doas -Z g{o..o}bby tasks list",
    "doas -- g{o..o}bby tasks list",
    "doas -s 'gobby sessions {approve-dirty-paths,} s7'",
    "doas -u root uv run g{o..o}bby tasks list",
)
BRACE_ALLOWED = (
    "find . -name x -exec gobby tasks list \\;",
    "find . -exec pytest tests/{a,b}.py \\;",
    "command -v gobby",
    "command -v gobby tasks list",
    "exec -a x gobby tasks list",
    "nice -n 5 gobby tasks list",
    "nice -n 5 uv run pytest tests/{a,b}.py",
    "timeout 5 gobby tasks list",
    "timeout -s TERM 5 gobby tasks list",
    "timeout --signal=TERM 5 gobby tasks list",
    "timeout 5s gobby tasks list",
    "timeout 5 uv run pytest tests/{a,b}.py",
    "xargs -I {} gobby tasks list",
    "xargs -I {} gobby tasks show {}",
    "xargs -n 1 gobby tasks list",
    "xargs -0 rm -f",
    "xargs -I '{}' gobby tasks show '{}'",
    "sh -c 'gobby tasks list'",
    'sh -c "echo hi"',
    "sh -c 'pytest tests/{a,b}.py'",
    "bash -c 'echo approve'",
    'gobby tasks "{show,list}"',
    "gobby tasks '{show,list}'",
    'gobby "tasks" "{a,b}" x',
    "gobby tasks \\{show,list\\}",
    "gobby \\{tasks,x\\} list",
    "gobby tasks show {a,b}",
    "gobby tasks create --data {x}",
    "gobby sessions list --project {a,b}",
    "ls src/gobby/{sessions,tasks}",
    "rg -g *.{py,md} sessions src",
    "echo {a,b}",
    "{ echo hi; }",
    'echo "gobby {tasks,x} list"',
    "git commit -m 'relay gobby {tasks,} list' -- a.txt",
    'gobby --config "{a,b}" tasks list',
    "gobby --config '/tmp/{x}' tasks show x",
    "gobby --config \\{a,b\\} tasks list",
    "gobby --config /tmp/x tasks show {a,b}",
    "gobby --config=/tmp/x tasks show {a,b}",
    "gobby --config /tmp/x tasks list",
    "uv run gobby tasks show {a,b}",
    "echo gobb{y..y}",
    "ls {gobby,x}.md",
    '"{gobby,x}" tasks list',
    "'{gobby,x}' tasks list",
    "\\{gobby,x\\} tasks list",
    "echo $HOME",
    "X=1 gobby tasks list",
    'X=1 Y="a b" gobby tasks list',
    "X='{a,b}' gobby tasks list",
    "X=$P gobby tasks list",
    'X=1 "gobby" tasks list',
    "X=1 'gobby' tasks list",
    'X=1 "{gobby,x}" tasks list',
    "X=1 '{gobby,x}' tasks list",
    "X=1 \\{gobby,x\\} tasks list",
    "X=1 gobby tasks show {a,b}",
    "X=1 gobby sessions list --project {a,b}",
    "uv run --project /tmp gobby tasks list",
    "uv run --project=/tmp gobby tasks list",
    "uv run --directory /repo gobby tasks list",
    "uv run -p 3.13 gobby tasks list",
    'uv run --project "$P" gobby tasks list',
    "uv run --with pytest pytest tests/{a,b}.py",
    "uv run --no-sync pytest tests/{a,b}.py",
    "uv run --project /tmp pytest {a,b}",
    "env -i PATH=/usr/bin gobby tasks list",
    "env X=1 gobby tasks list",
    "env -u HOME gobby tasks list",
    "env -C /tmp gobby tasks list",
    "env X=1 uv run pytest tests/{a,b}.py",
    'gobby --config "$X" tasks list',
    'gobby --config="$X" tasks list',
    'gobby tasks show "$ID"',
    "gobby tasks show $ID",
    'gobby "tasks" show {a,b}',
    'gobby sessions list --project "$P"',
    "'$x' tasks list",
    '"\\$x" tasks list',
    "uvx --from x gobby tasks list",
    "uvx --from=x gobby tasks list",
    "uvx -q gobby tasks list",
    "uvx --python 3.13 gobby tasks list",
    "uvx --with pytest gobby tasks list",
    'uvx --from "$X" gobby tasks list',
    "uv tool run --from x gobby tasks list",
    "nohup gobby tasks list",
    "nohup gobby sessions list",
    "time gobby tasks list",
    "time -p gobby tasks list",
    "time -o /tmp/t gobby tasks list",
    "time -o=/tmp/t gobby tasks list",
    "time -o/tmp/t gobby tasks list",
    'time -o "$F" gobby tasks list',
    "nohup time -p gobby tasks list",
    'time uv run --project "$P" gobby tasks list',
    "sudo gobby tasks list",
    "sudo -u root gobby tasks list",
    "sudo -u 'root' gobby tasks list",
    'sudo -u "$X" gobby tasks list',
    "sudo -n -u root gobby tasks list",
    "sudo -E gobby tasks list",
    "sudo -- gobby tasks list",
    "sudo -- gobby tasks show {a,b}",
    "sudo -h host gobby tasks list",
    "sudo --user=root gobby tasks list",
    "sudo --preserve-env=PATH gobby tasks list",
    "sudo -l",
    "sudo -v",
    "sudo -i gobby tasks list",
    "sudo -s echo hi",
    "sudo -u root pytest tests/{a,b}.py",
    "sudo -u root uv run pytest tests/{a,b}.py",
    "sudo nice -n 5 gobby tasks list",
    "doas gobby tasks list",
    "doas -u root gobby tasks list",
    "doas -n gobby tasks list",
    "doas -C /x gobby tasks list",
    "doas -L gobby tasks list",
    "doas -u root uv run pytest tests/{a,b}.py",
    "find . -exec sudo gobby tasks list \\;",
)


@pytest.mark.parametrize(
    "variables",
    [{"is_spawned_agent": True}, {}],
    ids=["spawned", "interactive"],
)
async def test_unquoted_brace_in_gobby_command_words_is_refused(
    temp_db: HubDatabase, variables: dict[str, object]
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute(
        "DELETE FROM rule_definitions WHERE name NOT IN (%s, %s)", (RULE_NAME, EXPANSION_RULE)
    )
    engine = RuleEngine(temp_db)
    decisions: dict[str, str] = {}
    for command in (*BRACE_REFUSED, *BRACE_ALLOWED):
        response = await engine.evaluate(
            _bash_event(command), session_id=SESSION_ID, variables=dict(variables)
        )
        decisions[command] = response.decision
    assert decisions == {
        **dict.fromkeys(BRACE_REFUSED, "block"),
        **dict.fromkeys(BRACE_ALLOWED, "allow"),
    }


def _root_value_options() -> set[str]:
    """Long options the root `cli` group takes a value for, read from its source."""
    tree = ast.parse(CLI_SOURCE.read_text())
    root = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cli"
    )
    names: set[str] = set()
    for decorator in root.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "option"
        ):
            continue
        switches = {
            keyword.arg
            for keyword in decorator.keywords
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        }
        if switches & {"is_flag", "count"}:
            continue
        names.update(
            arg.value
            for arg in decorator.args
            if isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.startswith("--")
        )
    return names


def _rule_value_options() -> set[str]:
    """Value-taking global options the approval rule skips, from its named group."""
    data = yaml.safe_load(RULE_FILE.read_text())
    (effect,) = [
        effect
        for effect in data["rules"][RULE_NAME]["effects"]
        if "value_options" in effect.get("command_pattern", "")
    ]
    (group,) = re.findall(r"\(\?P<value_options>([^)]*)\)", effect["command_pattern"])
    return set(group.split("|"))


def test_value_taking_global_options_match_the_root_cli_group() -> None:
    assert _root_value_options()
    assert _rule_value_options() == _root_value_options()
