"""The protected-branch guard's operator override is never available to an agent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-landing-override"
SESSION_ID = "44444444-4444-4444-8444-444444444444"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"


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
async def test_landing_override_is_blocked_for_every_session(
    temp_db: HubDatabase, variables: dict[str, object]
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("DELETE FROM rule_definitions WHERE name != %s", (RULE_NAME,))
    engine = RuleEngine(temp_db)

    async def decide(command: str) -> tuple[str, str]:
        response = await engine.evaluate(
            _bash_event(command), session_id=SESSION_ID, variables=dict(variables)
        )
        return response.decision, response.reason or ""

    override = await decide("GOBBY_LAND_COMMIT=1 git -C ../main merge --ff-only lane")
    exported = await decide("export GOBBY_LAND_COMMIT=1 && git update-ref refs/heads/main lane")
    plain = await decide("git -C ../main merge --ff-only lane")

    assert override[0] == "block"
    assert "gobby-tasks-ops:land_commit" in override[1]
    assert exported[0] == "block"
    assert plain[0] == "allow"


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("gcode grep -F 'GOBBY_LAND_COMMIT' src", "allow"),
        ("rg 'GOBBY_LAND_COMMIT=1' src", "allow"),
        ("rg GOBBY_LAND_COMMIT=1 src", "allow"),
        ("printf '%s' 'export GOBBY_LAND_COMMIT=1'", "allow"),
        ("env -u GOBBY_LAND_COMMIT git status", "allow"),
        ("env --help GOBBY_LAND_COMMIT=1", "allow"),
        ("env --version GOBBY_LAND_COMMIT=1", "allow"),
        ("declare -p GOBBY_LAND_COMMIT", "allow"),
        ("declare -xp GOBBY_LAND_COMMIT", "allow"),
        ("typeset -p GOBBY_LAND_COMMIT", "allow"),
        ("set -a; rg GOBBY_LAND_COMMIT=1 src", "allow"),
        ("set +k; rg GOBBY_LAND_COMMIT=1 src", "allow"),
        ("set -k; set +k; rg GOBBY_LAND_COMMIT=1 src", "allow"),
        ("bash -k -c true; rg GOBBY_LAND_COMMIT=1 src", "allow"),
        ("(set -k); rg GOBBY_LAND_COMMIT=1 src", "allow"),
        ("bash -n -c 'GOBBY_LAND_COMMIT=1 git commit'", "allow"),
        ("printf '%s' '((GOBBY_LAND_COMMIT = 1))'", "allow"),
        ("env SHELLOPTS=keyword bash +k -c 'rg GOBBY_LAND_COMMIT=1 src'", "allow"),
        (
            "set -k; export SHELLOPTS; env -u SHELLOPTS bash -c 'rg GOBBY_LAND_COMMIT=1 src'",
            "allow",
        ),
        (
            "set -k; export SHELLOPTS; env -i bash -c 'rg GOBBY_LAND_COMMIT=1 src'",
            "allow",
        ),
        (
            "set -k; export SHELLOPTS; uv run --no-sync bash -c 'git status GOBBY_LAND_COMMIT=1'",
            "block",
        ),
        (
            "env SHELLOPTS=keyword uv run --no-sync bash -c 'git status GOBBY_LAND_COMMIT=1'",
            "block",
        ),
        ("set -k; export SHELLOPTS; bash +k -c 'rg GOBBY_LAND_COMMIT=1 src'", "allow"),
        (
            "set -k; export SHELLOPTS; export -n SHELLOPTS; bash -c 'rg GOBBY_LAND_COMMIT=1 src'",
            "allow",
        ),
        ("cat <<'EOF'\nGOBBY_LAND_COMMIT=1 git commit\nEOF", "allow"),
        ("GOBBY_LAND_COMMIT=1 git commit", "block"),
        ("OTHER=value GOBBY_LAND_COMMIT='1' git commit", "block"),
        ("env 'GOBBY_LAND_COMMIT=1' git commit", "block"),
        ("env -S 'GOBBY_LAND_COMMIT=1 git commit'", "block"),
        ("sudo -u root GOBBY_LAND_COMMIT=1 git commit", "block"),
        ("export GOBBY_LAND_COMMIT=1", "block"),
        ("export GOBBY_LAND_COMMIT", "block"),
        ("declare -x GOBBY_LAND_COMMIT=1", "block"),
        ("typeset -x GOBBY_LAND_COMMIT=1", "block"),
        ("readonly GOBBY_LAND_COMMIT=1", "block"),
        ('X=GOBBY_LAND_COMMIT; export "$X=1"; git commit', "block"),
        ('X=GOBBY_LAND_COMMIT; env "$X=1" git commit', "block"),
        ('export X=GOBBY_LAND_COMMIT; env "$X=1" git commit', "block"),
        ('readonly X=GOBBY_LAND_COMMIT; export "$X=1"', "block"),
        ("declare -n r=GOBBY_LAND_COMMIT; r=1; export r; git commit", "block"),
        ("typeset -n r=GOBBY_LAND_COMMIT; r=1; export r", "block"),
        ('printf -v X "%s" GOBBY_LAND_COMMIT; export "$X=1"; git commit', "block"),
        ('printf -vX "%s" GOBBY_LAND_COMMIT; env "$X=1" git commit', "block"),
        (
            "shopt -s expand_aliases\nalias land='export GOBBY_LAND_COMMIT=1'\nland\ngit commit",
            "block",
        ),
        ("alias -p GOBBY_LAND_COMMIT", "allow"),
        ("alias GOBBY_LAND_COMMIT", "allow"),
        ("alias land-now='export GOBBY_LAND_COMMIT=1'\nland-now", "block"),
        ('set -- GOBBY_LAND_COMMIT; export "$1=1"; git commit', "block"),
        ('read X <<< GOBBY_LAND_COMMIT; export "$X=1"; git commit', "block"),
        ('echo GOBBY_LAND_COMMIT > f; read X < f; export "$X=1"; git commit', "block"),
        ("echo GOBBY_LAND_''COMMIT > f; read X < f; export \"$X=1\"; git commit", "block"),
        ('echo GOBBY_LAND_COMMIT > f; (read X < f; export "$X=1"; git commit)', "block"),
        (
            "echo GOBBY_LAND_COMMIT > f; bash -c 'read X < f; export \"$X=1\"; git commit'",
            "block",
        ),
        ("echo GOBBY_LAND_COMMIT | xargs -I{} env {}=1 git commit", "block"),
        ("read X <<'EOF'\nGOBBY_LAND_COMMIT\nEOF\nexport \"$X=1\"; git commit", "block"),
        ('read X <<< ORDINARY_NAME; export "$X=1"', "allow"),
        ("echo GOBBY_LAND_COMMIT | cat", "allow"),
        ("trap 'export GOBBY_LAND_COMMIT=1; git commit' EXIT", "block"),
        ("trap -p EXIT", "allow"),
        ("trap -p GOBBY_LAND_COMMIT", "allow"),
        ("gcode grep 'trap|alias|set --|GOBBY_LAND_COMMIT' tests/example.py", "allow"),
        ('printf -- "%s" "GOBBY_LAND_COMMIT"', "allow"),
        ("declare -p X=GOBBY_LAND_COMMIT", "allow"),
        ("printf '%s' 'X=GOBBY_LAND_COMMIT'", "allow"),
        ("bash -c 'GOBBY_LAND_COMMIT=1 git commit'", "block"),
        ("eval 'export GOBBY_LAND_COMMIT=1'", "block"),
        ("sh <<'EOF'\nGOBBY_LAND_COMMIT=1 git commit\nEOF", "block"),
        ('echo "$(GOBBY_LAND_COMMIT=1 git commit)"', "block"),
        ("printf x | env GOBBY_LAND_COMMIT=1 git commit", "block"),
        ("set -a; printf -v GOBBY_LAND_COMMIT %s 1; git commit", "block"),
        ("set -a; let GOBBY_LAND_COMMIT=1; git commit", "block"),
        ("set -a; read GOBBY_LAND_COMMIT; git commit", "block"),
        ("set -k; git commit GOBBY_LAND_COMMIT=1", "block"),
        ("set -o keyword; git commit GOBBY_LAND_COMMIT=1", "block"),
        ("bash -k -c 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("printf %s=%s GOBBY_LAND_COMMIT 1 | sh", "block"),
        ("set -a; (( GOBBY_LAND_COMMIT = 1 )); git commit", "block"),
        ("bash -a -c '(( GOBBY_LAND_COMMIT = 1 )); git commit'", "block"),
        ("eval 'set -k'; git commit GOBBY_LAND_COMMIT=1", "block"),
        ("env SHELLOPTS=keyword bash -c 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("bash -O extglob -k -c 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("bash --rcfile /dev/null -kc 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("set -k; export SHELLOPTS; bash -c 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("set -k; declare -x SHELLOPTS; bash -c 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("set -k; eval 'export SHELLOPTS'; bash -c 'git commit GOBBY_LAND_COMMIT=1'", "block"),
        ("set -k; false && set +k; rg GOBBY_LAND_COMMIT=1 src", "block"),
        ("set -a; (( 0 < (GOBBY_LAND_COMMIT = 1) )); git commit", "block"),
        ("if true; then GOBBY_LAND_COMMIT=1 git commit; fi", "block"),
        ('python -c \'import os; os.environ["GOBBY_LAND_COMMIT"] = "1"\'', "block"),
        pytest.param(
            "set -a; : ${GOBBY_LAND_COMMIT:=1}; git commit", "block", id="parameter-default-setter"
        ),
        pytest.param(
            'set -a; : "${GOBBY_LAND_COMMIT=1}"; git commit', "block", id="parameter-setter"
        ),
        pytest.param("rg '${GOBBY_LAND_COMMIT:=1}' src", "allow", id="parameter-literal-data"),
        pytest.param(r"rg \${GOBBY_LAND_COMMIT:=1} src", "allow", id="parameter-escaped-data"),
        pytest.param(
            "rg '${GOBBY_LAND_COMMIT:=1}'\"$OTHER\" src",
            "allow",
            id="parameter-mixed-literal-data",
        ),
        pytest.param(
            "set -a; : \"'${GOBBY_LAND_COMMIT:=1}'\"",
            "block",
            id="parameter-single-inside-double",
        ),
        pytest.param(
            "env SHELLOPTS=keyword uv run --no-sync rg GOBBY_LAND_COMMIT=1 src",
            "allow",
            id="delegated-argv-is-not-shell",
        ),
        pytest.param(
            "env SHELLOPTS=keyword env --unset=SHELLOPTS bash -c 'rg GOBBY_LAND_COMMIT=1 src'",
            "allow",
            id="nested-env-unset",
        ),
        pytest.param(
            "env SHELLOPTS=keyword env -iuSHELLOPTS bash -c 'rg GOBBY_LAND_COMMIT=1 src'",
            "allow",
            id="env-cluster-unset",
        ),
        pytest.param(
            "env -i SHELLOPTS=keyword bash -c 'git status GOBBY_LAND_COMMIT=1'",
            "block",
            id="env-clear-then-set",
        ),
        pytest.param(
            "env SHELLOPTS=keyword find . -exec bash -c 'git status GOBBY_LAND_COMMIT=1' \\;",
            "block",
            id="find-inherits-shellopts",
        ),
        pytest.param(
            "set -a; : ${GOBBY_LAND_COMMIT\\\n:=1}; git status",
            "block",
            id="parameter-line-continuation",
        ),
        pytest.param(
            "bash -n <<'EOF'\nGOBBY_LAND_COMMIT=1\nEOF",
            "allow",
            id="parse-only-heredoc",
        ),
        pytest.param("set -k; rg 'GOBBY_LAND_COMMIT=1' src", "allow", id="keyword-quoted-name"),
        pytest.param(
            "bash -n - <<'EOF'\nGOBBY_LAND_COMMIT=1\nEOF",
            "allow",
            id="parse-only-dash-heredoc",
        ),
        pytest.param(
            "set -a; exec git status <<EOF\n${GOBBY_LAND_COMMIT:=1}\nEOF",
            "block",
            id="parameter-unquoted-heredoc",
        ),
        pytest.param(
            "set -a; exec git status <<'EOF'\n${GOBBY_LAND_COMMIT:=1}\nEOF",
            "allow",
            id="parameter-quoted-heredoc",
        ),
        pytest.param(
            "set -a; exec git status <<EOF\n'${GOBBY_LAND_COMMIT:=1}'\nEOF",
            "block",
            id="parameter-heredoc-quotes-are-data",
        ),
        pytest.param(
            "set -a; git status <<EOF\n${GOBBY_LAND_COMMIT:=1}\nEOF",
            "block",
            id="parameter-direct-unquoted-heredoc",
        ),
        pytest.param(
            "set -a; git status <<'EOF'\n${GOBBY_LAND_COMMIT:=1}\nEOF",
            "allow",
            id="parameter-direct-quoted-heredoc",
        ),
        pytest.param(
            "set -a; git status <<EOF\n\\${GOBBY_LAND_COMMIT:=1}\nEOF",
            "allow",
            id="parameter-direct-escaped-heredoc",
        ),
    ],
)
async def test_landing_override_distinguishes_environment_from_data(
    temp_db: HubDatabase, command: str, decision: str
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("DELETE FROM rule_definitions WHERE name != %s", (RULE_NAME,))
    engine = RuleEngine(temp_db)

    response = await engine.evaluate(_bash_event(command), session_id=SESSION_ID, variables={})

    assert response.decision == decision
