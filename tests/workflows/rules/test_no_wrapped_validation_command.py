"""Tests for the bundled no-wrapped-validation-command rule.

The close gate credits a validation run only when the shell call is bare, so
this rule has to reject exactly the shapes that gate discards: pipelines, `;`
sequences, `||` fallbacks, backgrounding, and a trailing `echo`. Environment
prefixes, a leading `cd <dir> &&`, and `<check-a> && <check-b>` chains stay
allowed. The rule decides through the gate's own verdict rather than a
`command_pattern`, because a pattern is matched per shell segment and never
sees the separator that voids the call.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "no-wrapped-validation-command"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"

BLOCKED_COMMANDS = (
    "uv run mypy src/ 2>&1 | tail -3",
    "GOBBY_TEST_PROTECT=1 uv run pytest tests/x.py -q | tail -5",
    "uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json"
    " --fail-on-new 2>&1 | tail -3",
    "uv run ruff check src/; echo done",
    "cargo clippy -p gobby-terminal --all-targets -- -D warnings || true",
    "npx vitest run src/a.test.tsx && echo ok",
    "cd web && ./node_modules/.bin/vitest run src/a.test.tsx | cat",
    "cargo nextest run -p gobby-core &",
    "python -m pytest tests/x.py; printf done",
)

ALLOWED_COMMANDS = (
    "uv run mypy src/",
    "GOBBY_TEST_PROTECT=1 uv run pytest tests/x.py -q",
    "uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new",
    "uv run ruff check src/",
    "cargo clippy -p gobby-terminal --all-targets -- -D warnings",
    "npx vitest run src/a.test.tsx",
    "cargo nextest run -p gobby-core",
    "DATABASE_URL=x GOBBY_TEST_PROTECT=1 uv run pytest tests/x.py -q",
    "cd /repo && uv run mypy src/",
    "uv run ruff check src/ && uv run mypy src/",
    "uv run pytest tests/x.py -k 'a or b'",
    "uv run mypy src/ 2>&1",
    "cd web && ./node_modules/.bin/vitest run src/a.test.tsx",
    "git commit -m 'run pytest | tail'",
    "git log --oneline | head -5",
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> None:
    """Sync bundled rules from the real rules directory."""
    sync_bundled_rules(db, get_bundled_rules_path())


def _get_rule(manager: RuleDefinitionManager, name: str) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None, f"Rule {name!r} not found after sync"
    return RuleDefinitionBody.model_validate(row.definition_json)


def _tool_event(tool_name: str, data: dict[str, object]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=GOBBY_PROJECT_ID,
        data={"tool_name": tool_name, **data},
    )


def _bash_event(command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=GOBBY_PROJECT_ID,
        data={
            "command": command,
            "tool_input": {"command": command},
            "tool_name": "Bash",
        },
    )


def _isolated_engine(db: HubDatabase) -> RuleEngine:
    """Keep only this rule installed so a decision is attributable to it."""
    _sync_bundled(db)
    db.execute("DELETE FROM rule_definitions WHERE name != %s", (RULE_NAME,))
    return RuleEngine(db)


class TestRuleShape:
    def test_rule_is_a_bundled_before_tool_bash_block(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name(RULE_NAME)
        assert row is not None
        body = _get_rule(manager, RULE_NAME)

        assert body.event == "before_tool"
        assert row.enabled is True
        assert row.priority == 50
        effects = [effect for effect in body.resolved_effects if effect.type == "block"]
        assert len(effects) == 1
        assert effects[0].tools == ["Bash"]

    def test_reason_states_the_credit_rule_and_the_bare_rerun(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name(RULE_NAME)
        assert row is not None
        assert row.source == "installed"
        body = RuleDefinitionBody.model_validate(row.definition_json)
        reason = body.resolved_effects[0].reason or ""

        assert reason.startswith("Run the validation bare")
        assert "close-gate credit" in reason
        assert "`cargo nextest run -p gobby-client --status-level fail`" in reason

    def test_group_and_default_tag_match_the_worker_safety_family(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name(RULE_NAME)
        assert row is not None

        assert _get_rule(manager, RULE_NAME).group == "worker-safety"
        assert "default" in (row.tags or [])


class TestWrappedValidationBlocking:
    @pytest.mark.parametrize("command", BLOCKED_COMMANDS)
    async def test_blocks_wrapped_validation_runs(self, db: HubDatabase, command: str) -> None:
        engine = _isolated_engine(db)

        response = await engine.evaluate(_bash_event(command), session_id=SESSION_ID, variables={})

        assert response.decision == "block", f"should block: {command}"

    @pytest.mark.parametrize("command", ALLOWED_COMMANDS)
    async def test_allows_credited_and_unrelated_commands(
        self, db: HubDatabase, command: str
    ) -> None:
        engine = _isolated_engine(db)

        response = await engine.evaluate(_bash_event(command), session_id=SESSION_ID, variables={})

        assert response.decision != "block", f"should allow: {command}"

    @pytest.mark.parametrize(
        ("tool_name", "data"),
        [
            ("Read", {"tool_input": {"file_path": "src/gobby/app.py"}}),
            ("Read", {}),
            ("Bash", {"tool_input": None}),
            ("Bash", {"tool_input": "uv run mypy src/ | tail -3"}),
        ],
        ids=["other-tool", "no-tool-input", "null-tool-input", "string-tool-input"],
    )
    async def test_condition_never_fails_closed_on_foreign_tool_input(
        self, db: HubDatabase, tool_name: str, data: dict[str, object]
    ) -> None:
        """A condition error on this rule would block the call, so it must not error."""
        engine = _isolated_engine(db)

        response = await engine.evaluate(
            _tool_event(tool_name, data), session_id=SESSION_ID, variables={}
        )

        assert response.decision != "block"

    async def test_block_reason_reaches_the_agent(self, db: HubDatabase) -> None:
        engine = _isolated_engine(db)

        response = await engine.evaluate(
            _bash_event("uv run mypy src/ 2>&1 | tail -3"),
            session_id=SESSION_ID,
            variables={},
        )

        assert response.decision == "block"
        assert "close-gate credit" in (response.reason or "")
