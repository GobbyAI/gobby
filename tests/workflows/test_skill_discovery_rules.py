"""Tests for skill-discovery rules.

Verifies language skill requirements and reset-skill-injection rules sync correctly,
have valid structure, and evaluate conditions properly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.tool_error_tracker import extract_target_key, track_proxy_outcome
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.blocked_tool_recovery import _CODE_INDEX_REMEDIATION_RULES
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import (
    ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS,
    SafeExpressionEvaluator,
    build_condition_helpers,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _skill_tool_error_record(skill_name: str) -> dict[str, Any]:
    if skill_name.startswith("gobby:references/"):
        arguments = {"name": "gobby", "path": skill_name.split(":", 1)[1]}
        tool_name = "get_skill_file"
    else:
        arguments = {"name": skill_name}
        tool_name = "get_skill"
    timestamp = "2026-07-26T12:00:00+00:00"
    return {
        "tool": f"gobby-skills/{tool_name}",
        "target_key": extract_target_key({"tool_name": tool_name}, arguments),
        "error": "Workflow evaluation timed out after 15s",
        "first_at": timestamp,
        "last_at": timestamp,
        "count": 1,
    }


def _bundled_rule_condition(relative_path: str, rule_name: str) -> str:
    """Read a rule's `when` straight from its bundled template.

    Condition tests that hardcode a copy of the expression silently keep
    passing when the template drifts; reading the template keeps the
    evaluated expression and the shipped rule the same string.
    """
    from gobby.workflows.sync_rules import get_bundled_rules_path

    data = yaml.safe_load((get_bundled_rules_path() / relative_path).read_text(encoding="utf-8"))
    condition = data["rules"][rule_name]["when"]
    assert isinstance(condition, str)
    return condition


def _sync_bundled(db: HubDatabase) -> object:
    """Sync bundled rules and coerce source to 'installed' for test evaluation.

    sync_bundled_rules() imports templates with source='template', but the rule
    engine (list_rules_by_event) filters out template-sourced rules. Tests need
    rules to evaluate, so we coerce source to 'installed' to simulate activation
    via install_from_template().
    """
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


def _skill_fetch_template(name: str) -> str:
    return f'{{{{ skill_fetch_directive("{name}") }}}}'


SKILL_DISCOVERY_RULES = {
    "bootstrap-default-agent-core-skills",
    "clear-ocr-review-freshness",
    "list-skill-hubs-once-per-session",
    "require-bash-skill",
    "require-c-skill",
    "require-code-review-self-review",
    "require-code-review-skill",
    "require-cpp-skill",
    "require-csharp-skill",
    "require-dart-skill",
    "require-elixir-skill",
    "require-go-skill",
    "require-impeccable-skill",
    "require-java-skill",
    "require-javascript-skill",
    "require-json-skill",
    "require-kotlin-skill",
    "require-lua-skill",
    "require-objc-skill",
    "require-php-skill",
    "require-plan-skill",
    "require-python-skill",
    "require-ruby-skill",
    "require-rust-skill",
    "require-scala-skill",
    "require-swift-skill",
    "require-typescript-skill",
    "require-yaml-skill",
    "reset-skill-injection",
    "track-ocr-review-freshness",
}

LANGUAGE_SKILL_RULES = {
    rule_name
    for rule_name in SKILL_DISCOVERY_RULES
    if rule_name.startswith("require-")
    and rule_name
    not in {
        "require-code-review-self-review",
        "require-code-review-skill",
        "require-impeccable-skill",
        "require-plan-skill",
    }
}

REPLACED_SKILL_RULES = {
    "inject-python-skill",
    "inject-rust-skill",
    "block-and-teach-code-index",
    "discover-skill-hubs-on-turn-start",
}

BREVITY_RULES = {
    "opt-out-brevity",
    "inject-brevity-drift-feedback",
    "remind-brevity-on-turn-start",
    "detect-brevity-literal-drift",
    "detect-brevity-contrastive-drift",
}


# --- Sync tests ---


class TestSkillDiscoverySync:
    """Test that skill-discovery rules sync correctly."""

    def test_bundled_file_syncs_all_rules(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """All skill-discovery rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert SKILL_DISCOVERY_RULES.issubset(rule_names), (
            f"Missing: {SKILL_DISCOVERY_RULES - rule_names}"
        )
        assert REPLACED_SKILL_RULES.isdisjoint(rule_names)

        # The subset check above only catches an inventory entry that stopped
        # syncing. It cannot see a rule added to the directory and never listed
        # here, which is how require-objc-skill drifted out unnoticed. Compare
        # both directions against the group so the inventory stays exhaustive.
        synced_group = {
            row.name for row in rules if row.definition_json.get("group") == "skill-discovery"
        }
        assert synced_group == SKILL_DISCOVERY_RULES, (
            f"Unlisted in SKILL_DISCOVERY_RULES: {sorted(synced_group - SKILL_DISCOVERY_RULES)}; "
            f"listed but not synced: {sorted(SKILL_DISCOVERY_RULES - synced_group)}"
        )

    def test_all_rules_have_group(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        """All rules should have group='skill-discovery'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in SKILL_DISCOVERY_RULES:
                body = row.definition_json
                assert body.get("group") == "skill-discovery", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in SKILL_DISCOVERY_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                assert body.event is not None
                assert body.effects


class TestLanguageSkillWriteTargetContract:
    """Language rules inspect content-write targets, not every touched path."""

    @pytest.mark.parametrize("rule_name", sorted(LANGUAGE_SKILL_RULES))
    def test_rule_uses_canonical_write_target(self, rule_name: str) -> None:
        condition = _bundled_rule_condition(
            f"skill-discovery/{rule_name}.yaml",
            rule_name,
        )

        assert "event.data.get('canonical_write_file_path'" in condition

    def test_reset_skill_injection_clears_only_skill_ledgers(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-skill-injection")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.effects is not None

        set_variables = {
            effect.variable: effect.value
            for effect in body.effects
            if effect.type == "set_variable"
        }
        assert set_variables["loaded_skills"] == []
        assert "memory_nudge_fired" not in set_variables


# --- ordered context-epoch bootstrap ---


class TestDefaultAgentCoreSkillBootstrap:
    CORE_SKILLS = (
        "gobby:references/skills/loading.md",
        "gobby:references/memory/overview.md",
        "brevity",
        "restraint",
    )

    @staticmethod
    def _turn_event(prompt: str = "Continue.") -> HookEvent:
        return HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"prompt": prompt},
        )

    def test_structure_preserves_core_order(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("bootstrap-default-agent-core-skills")
        assert row is not None
        assert row.enabled is True
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "turn_start"
        assert body.effects is not None
        assert [effect.skill for effect in body.effects] == list(self.CORE_SKILLS)
        assert all(effect.type == "load_skill" for effect in body.effects)
        assert "handoff_pull_pending" in (body.when or "")
        assert "has_open_tool_error" in (body.when or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reset_source",
        [
            pytest.param(None, id="initial-start"),
            pytest.param("clear", id="clear"),
            pytest.param("compact", id="compact"),
        ],
    )
    async def test_bootstraps_missing_core_skills_each_context_epoch(
        self,
        db: HubDatabase,
        reset_source: str | None,
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "loaded_skills": list(self.CORE_SKILLS[2:]) if reset_source else [],
            "loaded_skill_references": list(self.CORE_SKILLS[:2]) if reset_source else [],
            "skill_discovery_instructions_shown": True,
            "_memory_initial_stop_checked": False,
            "open_tool_errors": [],
            "servers_listed": True,
        }
        if reset_source:
            reset = HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id=SESSION_ID,
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data={"source": reset_source},
            )
            await engine.evaluate(reset, session_id=SESSION_ID, variables=variables)
            assert variables["loaded_skills"] == []
            assert variables["loaded_skill_references"] == []

        response = await engine.evaluate(
            self._turn_event(),
            session_id=SESSION_ID,
            variables=variables,
        )

        context = response.context or ""
        directives = [skill_fetch_directive(skill) for skill in self.CORE_SKILLS]
        assert all(directive in context for directive in directives)
        assert [context.index(directive) for directive in directives] == sorted(
            context.index(directive) for directive in directives
        )

    @pytest.mark.asyncio
    async def test_memory_fetch_error_skips_only_memory(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        response = await RuleEngine(db).evaluate(
            self._turn_event(),
            session_id=SESSION_ID,
            variables={
                "loaded_skills": [],
                "skill_discovery_instructions_shown": True,
                "_memory_initial_stop_checked": False,
                "open_tool_errors": [
                    _skill_tool_error_record("gobby:references/memory/overview.md")
                ],
                "servers_listed": True,
            },
        )

        context = response.context or ""
        expected = [
            skill_fetch_directive(skill)
            for skill in ("gobby:references/skills/loading.md", "brevity", "restraint")
        ]
        assert skill_fetch_directive("gobby:references/memory/overview.md") not in context
        assert [context.index(directive) for directive in expected] == sorted(
            context.index(directive) for directive in expected
        )

    @pytest.mark.asyncio
    async def test_loaded_core_skills_stay_silent(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        response = await RuleEngine(db).evaluate(
            self._turn_event(),
            session_id=SESSION_ID,
            variables={
                "loaded_skills": list(self.CORE_SKILLS[2:]),
                "loaded_skill_references": list(self.CORE_SKILLS[:2]),
                "skill_discovery_instructions_shown": True,
                "_memory_initial_stop_checked": True,
                "servers_listed": True,
            },
        )

        context = response.context or ""
        assert all(skill_fetch_directive(skill) not in context for skill in self.CORE_SKILLS)

    @pytest.mark.asyncio
    async def test_brevity_opt_out_suppresses_bootstrap_load(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        response = await RuleEngine(db).evaluate(
            self._turn_event("stop brevity"),
            session_id=SESSION_ID,
            variables={
                "loaded_skill_references": list(self.CORE_SKILLS[:2]),
                "skill_discovery_instructions_shown": True,
                "_memory_initial_stop_checked": True,
                "brevity_disabled": False,
                "servers_listed": True,
            },
        )

        assert skill_fetch_directive("brevity") not in (response.context or "")


# --- once-per-session skill hub listing ---


class TestListSkillHubsOncePerSession:
    """Verify the once-per-session skill hub discovery rule."""

    def test_structure(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("list-skill-hubs-once-per-session")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "turn_start"
        assert "skill_discovery_instructions_shown" in (body.when or "")
        assert "handoff_pull_pending" in (body.when or "")
        assert body.effects is not None
        assert [effect.type for effect in body.effects] == ["mcp_call"]
        assert body.effects[0].server == "gobby-skills"
        assert body.effects[0].tool == "list_hubs"
        assert body.effects[0].inject_result is True
        assert body.effects[0].timeout_seconds == 2.0
        assert body.effects[0].success_variable == "skill_discovery_instructions_shown"
        assert getattr(body.effects[0], "delivery", None) == "on_receipt"

    @pytest.mark.asyncio
    async def test_injects_listing_once_and_sets_guard_after_success(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        calls = 0

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: Any
        ) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            assert server == "gobby-skills"
            assert tool == "list_hubs"
            assert args == {}
            return {
                "success": True,
                "result": {
                    "success": True,
                    "hubs": [
                        {
                            "name": "clawdhub",
                            "type": "clawdhub",
                            "auth_required": False,
                            "auth_configured": True,
                        }
                    ],
                },
            }

        variables: dict[str, Any] = {
            "loaded_skills": ["brevity"],
            "loaded_skill_references": [
                "gobby:references/skills/loading.md",
                "gobby:references/memory/overview.md",
            ],
            "servers_listed": True,
        }
        engine = RuleEngine(db, mcp_dispatcher=dispatcher)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "hi"},
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.context is not None
        assert skill_fetch_directive("gobby:references/skills/loading.md") not in response.context
        assert "<available-skill-hubs>" in response.context
        assert "- clawdhub (clawdhub, auth: not required)" in response.context
        assert variables["skill_discovery_instructions_shown"] is True
        assert calls == 1
        assert all(
            call.get("tool") != "list_hubs" for call in response.metadata.get("mcp_calls", [])
        )

    @pytest.mark.asyncio
    async def test_skips_while_handoff_pull_pending(self, db: HubDatabase) -> None:
        _sync_bundled(db)

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: Any
        ) -> dict[str, Any]:
            raise AssertionError(f"unexpected MCP call {server}/{tool}")

        variables: dict[str, Any] = {
            "handoff_pull_pending": True,
            "loaded_skills": [],
            "servers_listed": True,
        }
        engine = RuleEngine(db, mcp_dispatcher=dispatcher)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "Call get_handoff first."},
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert skill_fetch_directive("gobby:references/skills/loading.md") not in (
            response.context or ""
        )
        assert "skill_discovery_instructions_shown" not in variables

    @pytest.mark.asyncio
    async def test_does_not_set_guard_when_hub_listing_fails(self, db: HubDatabase) -> None:
        _sync_bundled(db)

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: Any
        ) -> dict[str, Any]:
            return {"success": False, "result": {"error": "hub manager unavailable"}}

        variables: dict[str, Any] = {
            "loaded_skills": ["brevity"],
            "loaded_skill_references": [
                "gobby:references/skills/loading.md",
                "gobby:references/memory/overview.md",
            ],
            "servers_listed": True,
        }
        engine = RuleEngine(db, mcp_dispatcher=dispatcher)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "hi"},
        )

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert variables.get("skill_discovery_instructions_shown") is None


# --- brevity rules ---


class TestBrevityRules:
    def _turn_variables(self, *, loaded: bool = True, disabled: bool = False) -> dict[str, Any]:
        return {
            "loaded_skills": ["brevity"] if loaded else [],
            "brevity_disabled": disabled,
            "brevity_level": "normal",
            "skill_discovery_instructions_shown": True,
            "_memory_initial_stop_checked": True,
            "servers_listed": True,
        }

    def test_brevity_rules_sync_and_retired_loaders_are_absent(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)

        rule_names = {r.name for r in manager.list_all()}

        assert BREVITY_RULES.issubset(rule_names)
        assert "inject-brevity-on-first-turn" not in rule_names
        assert "load-brevity-on-turn-start" not in rule_names

    def test_reinforce_brevity_structure(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)

        reminder_row = manager.get_by_name("remind-brevity-on-turn-start")
        assert reminder_row is not None
        reminder_body = RuleDefinitionBody.model_validate(reminder_row.definition_json)
        assert reminder_body.event.value == "turn_start"
        assert reminder_body.effects is not None
        assert reminder_body.effects[0].type == "inject_context"

    @pytest.mark.asyncio
    async def test_bootstrap_skips_brevity_while_handoff_pull_pending(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        variables: dict[str, Any] = self._turn_variables(loaded=False)
        variables["handoff_pull_pending"] = True
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "Call get_handoff first."},
        )

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)

        assert skill_fetch_directive("brevity") not in (response.context or "")

    def test_detect_brevity_contrastive_rule_uses_allowed_regex_patterns(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("detect-brevity-contrastive-drift")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        definition = body.when or ""
        for pattern in ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS:
            assert pattern in definition

    def test_brevity_session_variables_are_defined(self) -> None:
        import yaml

        from gobby.workflows.sync_rules import get_bundled_rules_path

        vars_path = get_bundled_rules_path().parent / "variables" / "gobby-default-variables.yaml"
        variables = yaml.safe_load(vars_path.read_text())["variables"]

        assert variables["brevity_disabled"]["value"] is False
        assert variables["brevity_level"]["value"] == "max"
        assert variables["restraint_disabled"]["value"] is False
        assert variables["restraint_level"]["value"] == "max"
        assert variables["brevity_last_violation"]["value"] == ""
        assert variables["brevity_last_violation_rule"]["value"] == ""
        assert variables["code_index_navigation_used_this_turn"]["value"] is False

    @pytest.mark.asyncio
    async def test_reinforcer_fires_once_per_epoch_after_brevity_is_loaded(
        self, db: HubDatabase
    ) -> None:
        """The reminder lands on the first turn of an epoch and stays quiet until the
        five-turn cadence (covered in test_reminder_cadence_rules) comes due."""
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=True)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "fix this"},
        )

        first = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        second = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        reminder = "Brevity reminder (normal): answer first; keep context tight."
        assert first.context is not None
        assert reminder in first.context
        assert variables["brevity_reminder_turn"] == 0
        assert second.context is None or reminder not in second.context

    @pytest.mark.asyncio
    async def test_opt_out_prompt_disables_and_suppresses_brevity_rules(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=False)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": " Stop Brevity "},
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert variables["brevity_disabled"] is True
        assert response.context is None or "brevity" not in response.context.lower()

    @pytest.mark.asyncio
    async def test_brevity_drift_persists_then_clears_on_next_turn(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=True)
        turn_end = HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.QWEN,
            timestamp=datetime.now(UTC),
            data={"response": "In summary, the fix is applied."},
        )

        await engine.evaluate(turn_end, session_id=SESSION_ID, variables=variables)

        assert variables["brevity_last_violation"] == "In summary"
        assert variables["brevity_last_violation_rule"] == "banned literal phrase"

        turn_start = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "next"},
        )

        response = await engine.evaluate(turn_start, session_id=SESSION_ID, variables=variables)

        assert response.context is not None
        assert "Brevity drift detected last turn" in response.context
        assert "`In summary`" in response.context
        assert variables["brevity_last_violation"] == ""
        assert variables["brevity_last_violation_rule"] == ""

    @pytest.mark.asyncio
    async def test_opt_out_suppresses_drift_detection(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=True, disabled=True)
        event = HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.QWEN,
            timestamp=datetime.now(UTC),
            data={"response": "In summary, the fix is applied."},
        )

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert "brevity_last_violation" not in variables


# --- require-python-skill structure ---


class TestRequirePythonSkillStructure:
    """Verify require-python-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-python-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('python')" in body.when
        assert "canonical_repo_mutation" in body.when
        assert ".pyi" in body.when
        assert "pyproject.toml" in body.when

    def test_has_block_effect_with_short_reason(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-python-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("python")


# --- require-python-skill condition evaluation ---


class TestRequirePythonSkillCondition:
    """Test the require-python-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('python') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and event.data.get('canonical_repo_mutation') "
        "and ("
        "event.data.get('canonical_write_file_path', '').endswith(('.py', '.pyi')) "
        "or event.data.get('canonical_write_file_path', '').rpartition('/')[2] "
        "in ('pyproject.toml', 'setup.cfg', 'setup.py', 'tox.ini', 'noxfile.py', "
        "'pytest.ini', 'mypy.ini', 'ruff.toml', '.python-version', 'Pipfile', "
        "'py.typed') "
        "or ("
        "event.data.get('canonical_write_file_path', '').rpartition('/')[2].startswith("
        "'requirements'"
        ") "
        "and event.data.get('canonical_write_file_path', '').rpartition('/')[2].endswith("
        "('.txt', '.in')"
        ")"
        ")"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        repo_mutation: bool = True,
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_repo_mutation": repo_mutation,
                    "canonical_write_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main.py",
            "/project/src/gobby/deep/module.py",
            "/project/src/package/__init__.py",
            "/project/src/package/models.pyi",
        ],
    )
    def test_matches_python_source_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/pyproject.toml",
            "/project/setup.cfg",
            "/project/setup.py",
            "/project/tox.ini",
            "/project/noxfile.py",
            "/project/pytest.ini",
            "/project/mypy.ini",
            "/project/ruff.toml",
            "/project/.python-version",
            "/project/Pipfile",
            "/project/src/package/py.typed",
            "/project/requirements.txt",
            "/project/requirements-dev.txt",
            "/project/requirements-dev.in",
        ],
    )
    def test_matches_python_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/config.yaml",
            "/project/src/main.js",
            "/project/package.json",
            "/project/pyproject.yaml",
            "/project/requirements.json",
            "/project/not_python.py.txt",
        ],
    )
    def test_skips_non_python_file(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_rust_file(self) -> None:
        assert self._eval("/project/src/main.rs") is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.py", loaded_skills=["python"]) is False

    def test_skips_python_write_outside_managed_project(self) -> None:
        assert self._eval("/tmp/scratchpad/helper.py", repo_mutation=False) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.py", injected_skills=["python"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="read") is False

    def test_skips_other_tool(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="execute") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-rust-skill structure ---


class TestRequireRustSkillStructure:
    """Verify require-rust-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-rust-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('rust')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-rust-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("rust")


# --- require-rust-skill condition evaluation ---


class TestRequireRustSkillCondition:
    """Test the require-rust-skill condition evaluates correctly."""

    condition: str

    @pytest.fixture(autouse=True)
    def _load_condition(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-rust-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        self.condition = body.when

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                    "canonical_write_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.condition)

    def test_matches_rust_write(self) -> None:
        assert self._eval("/project/src/main.rs") is True

    def test_matches_rust_edit(self) -> None:
        assert self._eval("/project/src/deep/lib.rs") is True

    def test_matches_cargo_manifest(self) -> None:
        assert self._eval("/project/Cargo.toml") is True

    def test_matches_cargo_lock(self) -> None:
        assert self._eval("/project/Cargo.lock") is True

    def test_matches_toolchain_file(self) -> None:
        assert self._eval("/project/rust-toolchain.toml") is True

    def test_matches_lint_and_format_config(self) -> None:
        assert self._eval("/project/clippy.toml") is True
        assert self._eval("/project/.rustfmt.toml") is True

    def test_matches_cargo_config(self) -> None:
        assert self._eval("/project/.cargo/config.toml") is True
        assert self._eval("/project/.cargo/config") is True
        assert self._eval(".cargo/config.toml") is True
        assert self._eval(".cargo/config") is True

    def test_skips_similar_cargo_suffix_without_path_boundary(self) -> None:
        assert self._eval("/project/foo.cargo/config") is False
        assert self._eval("/project/foo.cargo/config.toml") is False

    def test_skips_non_rust_file(self) -> None:
        assert self._eval("/project/config.yaml") is False

    def test_skips_unrelated_toml_file(self) -> None:
        assert self._eval("/project/settings.toml") is False

    def test_skips_python_file(self) -> None:
        assert self._eval("/project/src/main.py") is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.rs", loaded_skills=["rust"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.rs", injected_skills=["rust"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main.rs", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-javascript-skill structure ---


class TestRequireJavaScriptSkillStructure:
    """Verify require-javascript-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-javascript-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('javascript')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-javascript-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("javascript")


# --- require-javascript-skill condition evaluation ---


class TestRequireJavaScriptSkillCondition:
    """Test the require-javascript-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('javascript') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(('.js', '.jsx', '.mjs', '.cjs')) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] == 'package.json' "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] == 'jsconfig.json' "
        "or ("
        "event.data.get('canonical_file_path', '').rpartition('/')[2].startswith('jsconfig.') "
        "and event.data.get('canonical_file_path', '').rpartition('/')[2].endswith('.json')"
        ")"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main.js",
            "/project/src/component.jsx",
            "/project/src/entry.mjs",
            "/project/scripts/build.cjs",
            "/project/eslint.config.js",
        ],
    )
    def test_matches_javascript_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package.json",
            "/project/jsconfig.json",
            "/project/packages/web/jsconfig.browser.json",
        ],
    )
    def test_matches_javascript_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package-lock.json",
            "/project/src/main.ts",
            "/project/src/main.tsx",
            "/project/tsconfig.json",
            "/project/src/styles.css",
            "/project/jsconfig.json5",
        ],
    )
    def test_skips_non_javascript_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.js", loaded_skills=["javascript"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.js", injected_skills=["javascript"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main.js", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-dart-skill structure ---


class TestRequireDartSkillStructure:
    """Verify require-dart-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-dart-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('dart')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-dart-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("dart")


# --- require-dart-skill condition evaluation ---


class TestRequireDartSkillCondition:
    """Test the require-dart-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('dart') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith('.dart') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('pubspec.yaml', 'pubspec.lock', 'pubspec_overrides.yaml', "
        "'analysis_options.yaml', 'dart_test.yaml', 'build.yaml', "
        "'melos.yaml', 'l10n.yaml') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('flutter_launcher_icons.yaml', 'flutter_native_splash.yaml')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/lib/account/account_summary_screen.dart",
            "/project/test/account/account_summary_screen_test.dart",
            "/project/integration_test/app_test.dart",
        ],
    )
    def test_matches_dart_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/pubspec.yaml",
            "/project/pubspec.lock",
            "/project/pubspec_overrides.yaml",
            "/project/analysis_options.yaml",
            "/project/dart_test.yaml",
            "/project/build.yaml",
            "/project/melos.yaml",
            "/project/l10n.yaml",
            "/project/flutter_launcher_icons.yaml",
            "/project/flutter_native_splash.yaml",
        ],
    )
    def test_matches_dart_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package.json",
            "/project/pubspec.yaml.bak",
            "/project/lib/account/AccountScreen.kt",
            "/project/assets/config.json",
            "/project/android/app/build.gradle",
        ],
    )
    def test_skips_non_dart_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/lib/main.dart", loaded_skills=["dart"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/lib/main.dart", injected_skills=["dart"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/lib/main.dart", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-c-skill structure ---


class TestRequireCSkillStructure:
    """Verify require-c-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-c-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('c')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-c-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("c")


# --- require-c-skill condition evaluation ---


class TestRequireCSkillCondition:
    """Test the require-c-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('c') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(("
        "'.c', '.h', '.c.in', '.h.in', '.pc', '.pc.in'"
        ")) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('Makefile', 'makefile', 'GNUmakefile', 'CMakeLists.txt', "
        "'meson.build', 'meson_options.txt') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('configure.ac', 'configure.in', 'Makefile.am', 'Makefile.in', "
        "'compile_flags.txt', 'compile_commands.json') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('.clang-format', '.clang-tidy', '.clangd')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/account_record.c",
            "/project/include/account_record.h",
            "/project/generated/config.h.in",
            "/project/pkg/libaccounts.pc.in",
        ],
    )
    def test_matches_c_source_and_header_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Makefile",
            "/project/makefile",
            "/project/GNUmakefile",
            "/project/CMakeLists.txt",
            "/project/meson.build",
            "/project/meson_options.txt",
            "/project/configure.ac",
            "/project/configure.in",
            "/project/Makefile.am",
            "/project/Makefile.in",
            "/project/compile_flags.txt",
            "/project/compile_commands.json",
            "/project/.clang-format",
            "/project/.clang-tidy",
            "/project/.clangd",
        ],
    )
    def test_matches_c_build_and_analysis_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/account_record.cpp",
            "/project/include/account_record.hpp",
            "/project/src/account_record.cc",
            "/project/src/account_record.cxx",
            "/project/src/AccountRecord.cs",
            "/project/package.json",
            "/project/appsettings.json",
            "/project/.editorconfig",
            "/project/config.yaml",
        ],
    )
    def test_skips_non_c_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/account_record.c", loaded_skills=["c"]) is False

    def test_csharp_skill_does_not_count_as_c_loaded(self) -> None:
        assert self._eval("/project/src/account_record.c", loaded_skills=["csharp"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/account_record.c", injected_skills=["c"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/account_record.c", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-cpp-skill structure ---


class TestRequireCppSkillStructure:
    """Verify require-cpp-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-cpp-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('cpp')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-cpp-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("cpp")


# --- require-cpp-skill condition evaluation ---


class TestRequireCppSkillCondition:
    """Test the require-cpp-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('cpp') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(("
        "'.cpp', '.cc', '.cxx', '.c++', '.hpp', '.hh', '.hxx', '.h++', "
        "'.ipp', '.ixx', '.tpp', '.inl', '.cu', '.cuh', '.C'"
        ")) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('CMakeLists.txt', 'CMakePresets.json', 'CMakeUserPresets.json', "
        "'meson.build', 'meson_options.txt', 'conanfile.txt', 'conanfile.py', "
        "'vcpkg.json', 'vcpkg-configuration.json') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('compile_flags.txt', 'compile_commands.json', 'BUILD', "
        "'BUILD.bazel', 'WORKSPACE', 'MODULE.bazel') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('.clang-format', '.clang-tidy', '.clangd')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/account_record.cpp",
            "/project/src/account_record.cc",
            "/project/src/account_record.cxx",
            "/project/src/account_record.c++",
            "/project/include/account_record.hpp",
            "/project/include/account_record.hh",
            "/project/include/account_record.hxx",
            "/project/include/account_record.h++",
            "/project/include/account_record.ipp",
            "/project/modules/account_record.ixx",
            "/project/include/account_record.tpp",
            "/project/include/account_record.inl",
            "/project/cuda/account_record.cu",
            "/project/cuda/account_record.cuh",
            "/project/src/account_record.C",
        ],
    )
    def test_matches_cpp_source_header_module_and_cuda_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/CMakeLists.txt",
            "/project/CMakePresets.json",
            "/project/CMakeUserPresets.json",
            "/project/meson.build",
            "/project/meson_options.txt",
            "/project/conanfile.txt",
            "/project/conanfile.py",
            "/project/vcpkg.json",
            "/project/vcpkg-configuration.json",
            "/project/compile_flags.txt",
            "/project/compile_commands.json",
            "/project/BUILD",
            "/project/BUILD.bazel",
            "/project/WORKSPACE",
            "/project/MODULE.bazel",
            "/project/.clang-format",
            "/project/.clang-tidy",
            "/project/.clangd",
        ],
    )
    def test_matches_cpp_build_package_and_analysis_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/account_record.c",
            "/project/include/account_record.h",
            "/project/generated/config.h.in",
            "/project/pkg/libaccounts.pc.in",
            "/project/src/AccountRecord.cs",
            "/project/package.json",
            "/project/appsettings.json",
            "/project/.editorconfig",
            "/project/config.yaml",
            "/project/CMakeLists.txt.bak",
        ],
    )
    def test_skips_non_cpp_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/account_record.cpp", loaded_skills=["cpp"]) is False

    def test_c_skill_does_not_count_as_cpp_loaded(self) -> None:
        assert self._eval("/project/src/account_record.cpp", loaded_skills=["c"]) is True

    def test_csharp_skill_does_not_count_as_cpp_loaded(self) -> None:
        assert self._eval("/project/src/account_record.cpp", loaded_skills=["csharp"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/account_record.cpp", injected_skills=["cpp"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/account_record.cpp", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-elixir-skill structure ---


class TestRequireElixirSkillStructure:
    """Verify require-elixir-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-elixir-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('elixir')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-elixir-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("elixir")


# --- require-elixir-skill condition evaluation ---


class TestRequireElixirSkillCondition:
    """Test the require-elixir-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('elixir') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(("
        "'.ex', '.exs', '.eex', '.heex', '.leex', '.sface', '.livemd'"
        ")) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('mix.exs', 'mix.lock', '.formatter.exs', '.credo.exs', "
        "'.dialyzer_ignore.exs', '.iex.exs') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('config.exs', 'runtime.exs', 'dev.exs', 'test.exs', 'prod.exs', "
        "'releases.exs', 'seeds.exs')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/lib/accounts/notification_worker.ex",
            "/project/test/accounts/notification_worker_test.exs",
            "/project/lib/app_web/live/dashboard_live.html.heex",
            "/project/lib/app_web/templates/page/index.html.eex",
            "/project/lib/app_web/templates/page/index.html.leex",
            "/project/lib/app_web/components/button.sface",
            "/project/notebooks/reconciliation.livemd",
        ],
    )
    def test_matches_elixir_source_template_and_notebook_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/mix.exs",
            "/project/mix.lock",
            "/project/.formatter.exs",
            "/project/.credo.exs",
            "/project/.dialyzer_ignore.exs",
            "/project/.iex.exs",
            "/project/config/config.exs",
            "/project/config/runtime.exs",
            "/project/config/dev.exs",
            "/project/config/test.exs",
            "/project/config/prod.exs",
            "/project/rel/releases.exs",
            "/project/priv/repo/seeds.exs",
        ],
    )
    def test_matches_elixir_mix_and_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/accounts.erl",
            "/project/include/accounts.hrl",
            "/project/lib/accounts.exs.bak",
            "/project/mix.exs.bak",
            "/project/package.json",
            "/project/appsettings.json",
            "/project/.editorconfig",
            "/project/config.yaml",
        ],
    )
    def test_skips_non_elixir_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/lib/accounts.ex", loaded_skills=["elixir"]) is False

    def test_erlang_skill_does_not_count_as_elixir_loaded(self) -> None:
        assert self._eval("/project/lib/accounts.ex", loaded_skills=["erlang"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/lib/accounts.ex", injected_skills=["elixir"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/lib/accounts.ex", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-ruby-skill structure ---


class TestRequireRubySkillStructure:
    """Verify require-ruby-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-ruby-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('ruby')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-ruby-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("ruby")


# --- require-ruby-skill condition evaluation ---


class TestRequireRubySkillCondition:
    """Test the require-ruby-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('ruby') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(("
        "'.rb', '.rake', '.gemspec', '.ru', '.erb', '.rbs', '.jbuilder', "
        "'.builder', '.haml', '.slim'"
        ")) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('Gemfile', 'Gemfile.lock', 'gems.rb', 'gems.locked', 'Rakefile', "
        "'Guardfile', 'Capfile', 'Dangerfile', 'Podfile', 'Brewfile', 'Fastfile', "
        "'Appfile', 'Pluginfile', 'Deliverfile', 'Matchfile', 'Snapfile', "
        "'Scanfile', 'Gymfile', 'config.ru', '.ruby-version', '.ruby-gemset', "
        "'.rspec', '.rubocop.yml', '.rubocop_todo.yml', '.standard.yml', "
        "'Steepfile') "
        "or event.data.get('canonical_file_path', '').endswith('sorbet/config')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/app/models/account.rb",
            "/project/lib/tasks/backfill.rake",
            "/project/my_gem.gemspec",
            "/project/config.ru",
            "/project/app/views/accounts/show.html.erb",
            "/project/sig/account.rbs",
            "/project/app/views/accounts/show.json.jbuilder",
            "/project/app/views/accounts/show.builder",
            "/project/app/views/accounts/show.html.haml",
            "/project/app/views/accounts/show.html.slim",
        ],
    )
    def test_matches_ruby_source_template_and_signature_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Gemfile",
            "/project/Gemfile.lock",
            "/project/gems.rb",
            "/project/gems.locked",
            "/project/Rakefile",
            "/project/Guardfile",
            "/project/Capfile",
            "/project/Dangerfile",
            "/project/Podfile",
            "/project/Brewfile",
            "/project/fastlane/Fastfile",
            "/project/fastlane/Appfile",
            "/project/fastlane/Pluginfile",
            "/project/fastlane/Deliverfile",
            "/project/fastlane/Matchfile",
            "/project/fastlane/Snapfile",
            "/project/fastlane/Scanfile",
            "/project/fastlane/Gymfile",
            "/project/.ruby-version",
            "/project/.ruby-gemset",
            "/project/.rspec",
            "/project/.rubocop.yml",
            "/project/.rubocop_todo.yml",
            "/project/.standard.yml",
            "/project/Steepfile",
            "/project/sorbet/config",
            "sorbet/config",
        ],
    )
    def test_matches_ruby_tooling_and_dsl_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/app/models/account.rb.bak",
            "/project/Gemfile.bak",
            "/project/package.json",
            "/project/go.mod",
            "/project/mix.exs",
            "/project/config/database.yml",
            "/project/config/settings.yaml",
            "/project/.editorconfig",
            "/project/Dockerfile",
        ],
    )
    def test_skips_non_ruby_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/app/models/account.rb", loaded_skills=["ruby"]) is False

    def test_rails_skill_does_not_count_as_ruby_loaded(self) -> None:
        assert self._eval("/project/app/models/account.rb", loaded_skills=["rails"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/app/models/account.rb", injected_skills=["ruby"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/app/models/account.rb", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-csharp-skill structure ---


class TestRequireCSharpSkillStructure:
    """Verify require-csharp-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-csharp-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('csharp')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-csharp-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("csharp")


# --- require-csharp-skill condition evaluation ---


class TestRequireCSharpSkillCondition:
    """Test the require-csharp-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('csharp') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(("
        "'.cs', '.csx', '.csproj', '.sln', '.slnx', '.razor', '.cshtml', '.cake'"
        ")) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('global.json', 'nuget.config', 'NuGet.Config', 'packages.lock.json', "
        "'dotnet-tools.json', 'omnisharp.json', 'stylecop.json') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('Directory.Build.props', 'Directory.Build.targets', 'Directory.Packages.props')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/Accounts/AccountSummaryService.cs",
            "/project/src/Accounts/Program.cs",
            "/project/build/build.cake",
            "/project/src/App/App.razor",
            "/project/src/App/Views/Home.cshtml",
        ],
    )
    def test_matches_csharp_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/App.sln",
            "/project/App.slnx",
            "/project/src/Accounts/Accounts.Api.csproj",
            "/project/global.json",
            "/project/NuGet.Config",
            "/project/nuget.config",
            "/project/packages.lock.json",
            "/project/.config/dotnet-tools.json",
            "/project/omnisharp.json",
            "/project/stylecop.json",
            "/project/Directory.Build.props",
            "/project/Directory.Build.targets",
            "/project/Directory.Packages.props",
        ],
    )
    def test_matches_dotnet_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package.json",
            "/project/appsettings.json",
            "/project/.editorconfig",
            "/project/Directory.Build.props.bak",
            "/project/src/Accounts/AccountSummary.java",
            "/project/src/Accounts/project.xml",
        ],
    )
    def test_skips_non_csharp_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/App/Program.cs", loaded_skills=["csharp"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/App/Program.cs", injected_skills=["csharp"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/App/Program.cs", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-go-skill structure ---


class TestRequireGoSkillStructure:
    """Verify require-go-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-go-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('go')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-go-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("go")


# --- require-go-skill condition evaluation ---


class TestRequireGoSkillCondition:
    """Test the require-go-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('go') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith('.go') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('go.mod', 'go.sum', 'go.work', 'go.work.sum') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('.golangci.yml', '.golangci.yaml', 'golangci.yml', 'golangci.yaml')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/main.go",
            "/project/internal/profile/client.go",
            "/project/cmd/server/main_test.go",
        ],
    )
    def test_matches_go_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/go.mod",
            "/project/go.sum",
            "/project/go.work",
            "/project/go.work.sum",
            "/project/.golangci.yml",
            "/project/.golangci.yaml",
            "/project/golangci.yml",
            "/project/golangci.yaml",
        ],
    )
    def test_matches_go_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main.ts",
            "/project/src/main.js",
            "/project/go.mod.bak",
            "/project/.golangci.toml",
            "/project/Dockerfile",
        ],
    )
    def test_skips_non_go_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/main.go", loaded_skills=["go"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/main.go", injected_skills=["go"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/main.go", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-java-skill structure ---


class TestRequireJavaSkillStructure:
    """Verify require-java-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-java-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('java')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-java-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("java")


# --- require-java-skill condition evaluation ---


class TestRequireJavaSkillCondition:
    """Test the require-java-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('java') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith('.java') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('pom.xml', 'build.gradle', 'build.gradle.kts', 'settings.gradle', "
        "'settings.gradle.kts', 'gradle.properties') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('maven.config', 'jvm.config', 'checkstyle.xml', 'pmd.xml', "
        "'spotbugs-exclude.xml', 'lombok.config')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main/java/com/acme/ProfileClient.java",
            "/project/src/test/java/com/acme/ProfileClientTest.java",
            "/project/module-info.java",
        ],
    )
    def test_matches_java_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/pom.xml",
            "/project/service/pom.xml",
            "/project/build.gradle",
            "/project/build.gradle.kts",
            "/project/settings.gradle",
            "/project/settings.gradle.kts",
            "/project/gradle.properties",
            "/project/.mvn/maven.config",
            "/project/.mvn/jvm.config",
            "/project/checkstyle.xml",
            "/project/pmd.xml",
            "/project/spotbugs-exclude.xml",
            "/project/lombok.config",
        ],
    )
    def test_matches_java_build_and_static_analysis_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main/kotlin/com/acme/ProfileClient.kt",
            "/project/src/main/groovy/com/acme/ProfileClient.groovy",
            "/project/package.json",
            "/project/build.gradle.bak",
            "/project/application.yml",
            "/project/gradle/libs.versions.toml",
        ],
    )
    def test_skips_non_java_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main/java/Main.java", loaded_skills=["java"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main/java/Main.java", injected_skills=["java"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main/java/Main.java", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-kotlin-skill structure ---


class TestRequireKotlinSkillStructure:
    """Verify require-kotlin-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-kotlin-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('kotlin')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-kotlin-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("kotlin")


# --- require-kotlin-skill condition evaluation ---


class TestRequireKotlinSkillCondition:
    """Test the require-kotlin-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('kotlin') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(('.kt', '.kts')) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('.kotlin-version', 'detekt.yml', 'detekt.yaml', "
        "'.detekt.yml', '.detekt.yaml', 'ktlint.yml', 'ktlint.yaml', '.ktlint.yml', "
        "'.ktlint.yaml') "
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main/kotlin/com/acme/Profile.kt",
            "/project/build.gradle.kts",
            "/project/settings.gradle.kts",
            "/project/build-logic/src/main/kotlin/acme.kotlin-conventions.gradle.kts",
            "/project/scripts/migrate.main.kts",
        ],
    )
    def test_matches_kotlin_source_and_script_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.kotlin-version",
            "/project/detekt.yml",
            "/project/config/detekt.yaml",
            "/project/.detekt.yml",
            "/project/.detekt.yaml",
            "/project/ktlint.yml",
            "/project/config/ktlint.yaml",
            "/project/.ktlint.yml",
            "/project/.ktlint.yaml",
        ],
    )
    def test_matches_kotlin_tooling_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main/java/com/acme/Profile.java",
            "/project/build.gradle",
            "/project/settings.gradle",
            "/project/gradle.properties",
            "/project/gradle/libs.versions.toml",
            "/project/pom.xml",
            "/project/package.json",
            "/project/.editorconfig",
            "/project/src/main/kotlin/Profile.kt.bak",
        ],
    )
    def test_skips_non_kotlin_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main/kotlin/Profile.kt", loaded_skills=["kotlin"]) is False

    def test_java_skill_does_not_count_as_kotlin_loaded(self) -> None:
        assert self._eval("/project/src/main/kotlin/Profile.kt", loaded_skills=["java"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main/kotlin/Profile.kt", injected_skills=["kotlin"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert (
            self._eval("/project/src/main/kotlin/Profile.kt", canonical_tool_kind="read") is False
        )

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-scala-skill structure ---


class TestRequireScalaSkillStructure:
    """Verify require-scala-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-scala-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('scala')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-scala-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("scala")


# --- require-scala-skill condition evaluation ---


class TestRequireScalaSkillCondition:
    """Test the require-scala-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('scala') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(('.scala', '.sc', '.sbt')) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('.scala-version', '.scalafmt.conf', '.scalafix.conf', '.sbtopts') "
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main/scala/com/acme/orders/Order.scala",
            "/project/scripts/migrate.sc",
            "/project/build.sc",
            "/project/build.sbt",
            "/project/project/plugins.sbt",
        ],
    )
    def test_matches_scala_source_script_and_build_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.scala-version",
            "/project/.scalafmt.conf",
            "/project/config/.scalafix.conf",
            "/project/.sbtopts",
        ],
    )
    def test_matches_scala_tooling_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main/java/com/acme/orders/Order.java",
            "/project/src/main/kotlin/com/acme/orders/Order.kt",
            "/project/build.gradle.kts",
            "/project/pom.xml",
            "/project/.jvmopts",
            "/project/package.json",
            "/project/src/main/scala/Order.scala.bak",
        ],
    )
    def test_skips_non_scala_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main/scala/Order.scala", loaded_skills=["scala"]) is False

    def test_kotlin_skill_does_not_count_as_scala_loaded(self) -> None:
        assert self._eval("/project/src/main/scala/Order.scala", loaded_skills=["kotlin"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main/scala/Order.scala", injected_skills=["scala"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert (
            self._eval("/project/src/main/scala/Order.scala", canonical_tool_kind="read") is False
        )

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-lua-skill structure ---


class TestRequireLuaSkillStructure:
    """Verify require-lua-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-lua-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('lua')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-lua-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("lua")


# --- require-lua-skill condition evaluation ---


class TestRequireLuaSkillCondition:
    """Test the require-lua-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('lua') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(('.lua', '.rockspec')) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('.busted', '.lua-format', '.luacheckrc', '.luacov', '.luarc.json', "
        "'.luarc.jsonc', '.stylua.toml', 'stylua.toml', 'selene.toml') "
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/policy.lua",
            "/project/lua/acme/init.lua",
            "/project/acme-1.0-1.rockspec",
        ],
    )
    def test_matches_lua_source_and_rockspec_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.busted",
            "/project/.lua-format",
            "/project/.luacheckrc",
            "/project/.luacov",
            "/project/.luarc.json",
            "/project/config/.luarc.jsonc",
            "/project/.stylua.toml",
            "/project/stylua.toml",
            "/project/selene.toml",
        ],
    )
    def test_matches_lua_tooling_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/policy.c",
            "/project/src/policy.py",
            "/project/src/policy.luau",
            "/project/src/policy.tl",
            "/project/package.json",
            "/project/src/policy.lua.bak",
            "/project/config/stylua.toml.example",
        ],
    )
    def test_skips_non_lua_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/policy.lua", loaded_skills=["lua"]) is False

    def test_javascript_skill_does_not_count_as_lua_loaded(self) -> None:
        assert self._eval("/project/src/policy.lua", loaded_skills=["javascript"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/policy.lua", injected_skills=["lua"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/policy.lua", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


class TestRequireObjcSkillStructure:
    """Verify require-objc-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-objc-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('objc')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-objc-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("objc")


class TestRequireObjcSkillCondition:
    """Test the require-objc-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('objc') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and event.data.get('canonical_file_path', '').endswith(('.m', '.mm', '.h', '.pch'))"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Sources/ProfileKit/ProfileClient.m",
            "/project/Sources/ProfileKit/EngineBridge.mm",
            "/project/Sources/ProfileKit/ProfileClient.h",
            "/project/Sources/ProfileKit/ProfileKit.pch",
        ],
    )
    def test_matches_objc_source_header_and_prefix_header_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Sources/ProfileKit/ProfileClient.c",
            "/project/Sources/ProfileKit/ProfileClient.cc",
            "/project/Sources/ProfileKit/ProfileClient.cpp",
            "/project/Sources/ProfileKit/ProfileClient.hpp",
            "/project/Sources/ProfileKit/ProfileClient.swift",
            "/project/Sources/ProfileKit/module.modulemap",
            "/project/Sources/ProfileKit/ProfileClient.m.bak",
            "/project/ProfileKit.xcodeproj/project.pbxproj",
        ],
    )
    def test_skips_non_objc_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/ProfileClient.m", loaded_skills=["objc"]) is False

    def test_swift_skill_does_not_count_as_objc_loaded(self) -> None:
        assert self._eval("/project/ProfileClient.m", loaded_skills=["swift"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/ProfileClient.m", injected_skills=["objc"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/ProfileClient.m", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-swift-skill structure ---


class TestRequireSwiftSkillStructure:
    """Verify require-swift-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-swift-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('swift')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-swift-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("swift")


# --- require-swift-skill condition evaluation ---


class TestRequireSwiftSkillCondition:
    """Test the require-swift-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('swift') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith('.swift') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('Package.swift', 'Package.resolved', '.swift-version', '.swiftlint.yml', "
        "'.swiftlint.yaml', 'swiftlint.yml', 'swiftlint.yaml', '.swiftformat', "
        "'.swift-format', 'swift-format.json', '.swift-format.json') "
        "or ("
        "'.xcodeproj/' in event.data.get('canonical_file_path', '') "
        "and event.data.get('canonical_file_path', '').rpartition('/')[2] == 'project.pbxproj'"
        ")"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Sources/ProfileCore/Profile.swift",
            "/project/Tests/ProfileCoreTests/ProfileTests.swift",
            "/project/Sources/ProfileFeature/ProfileViewModel.swift",
            "/project/Plugins/ProfilePlugin/plugin.swift",
        ],
    )
    def test_matches_swift_source_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Package.swift",
            "/project/Package.resolved",
            "/project/.swift-version",
            "/project/.swiftlint.yml",
            "/project/config/.swiftlint.yaml",
            "/project/swiftlint.yml",
            "/project/config/swiftlint.yaml",
            "/project/.swiftformat",
            "/project/.swift-format",
            "/project/swift-format.json",
            "/project/.swift-format.json",
            "/project/ProfileApp.xcodeproj/project.pbxproj",
        ],
    )
    def test_matches_swift_package_tooling_and_xcode_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/Sources/ProfileCore/Profile.m",
            "/project/Sources/ProfileCore/Profile.mm",
            "/project/Sources/ProfileCore/Profile.h",
            "/project/Info.plist",
            "/project/ProfileApp.xcworkspace/contents.xcworkspacedata",
            "/project/project.pbxproj",
            "/project/Package.swift.bak",
            "/project/.swiftlint.json",
            "/project/tsconfig.json",
            "/project/src/main.kt",
        ],
    )
    def test_skips_non_swift_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/Sources/Profile.swift", loaded_skills=["swift"]) is False

    def test_kotlin_skill_does_not_count_as_swift_loaded(self) -> None:
        assert self._eval("/project/Sources/Profile.swift", loaded_skills=["kotlin"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/Sources/Profile.swift", injected_skills=["swift"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/Sources/Profile.swift", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-yaml-skill structure ---


class TestRequireYamlSkillStructure:
    """Verify require-yaml-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-yaml-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('yaml')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-yaml-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("yaml")


class TestRequireYamlSkillCondition:
    """Test the require-yaml-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('yaml') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith('.yaml') "
        "or event.data.get('canonical_file_path', '').endswith('.yml') "
        "or event.data.get('canonical_file_path', '').endswith('.yaml.j2') "
        "or event.data.get('canonical_file_path', '').endswith('.yml.j2') "
        "or event.data.get('canonical_file_path', '').endswith('.yaml.tpl') "
        "or event.data.get('canonical_file_path', '').endswith('.yml.tpl') "
        "or event.data.get('canonical_file_path', '').endswith('.yaml.tmpl') "
        "or event.data.get('canonical_file_path', '').endswith('.yml.tmpl') "
        "or event.data.get('canonical_file_path', '').endswith('.yaml.template') "
        "or event.data.get('canonical_file_path', '').endswith('.yml.template') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] == '.yamllint'"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/config/app.yaml",
            "/project/config/app.yml",
            "/project/.github/workflows/deploy.yml",
            "/project/charts/api/values.yaml",
            "/project/openapi/spec.yaml",
            "/project/docker-compose.yml",
        ],
    )
    def test_matches_yaml_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/templates/deployment.yaml.j2",
            "/project/templates/deployment.yml.j2",
            "/project/templates/config.yaml.tpl",
            "/project/templates/config.yml.tpl",
            "/project/templates/values.yaml.tmpl",
            "/project/templates/values.yml.tmpl",
            "/project/templates/workflow.yaml.template",
            "/project/templates/workflow.yml.template",
        ],
    )
    def test_matches_yaml_template_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.yamllint",
            "/project/config/.yamllint",
        ],
    )
    def test_matches_yaml_tool_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/config/app.yaml.bak",
            "/project/config/app.yml.disabled",
            "/project/config/app.json",
            "/project/config/app.toml",
            "/project/.prettierrc",
            "/project/src/main.py",
            "/project/src/App.tsx",
        ],
    )
    def test_skips_non_yaml_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/config/app.yaml", loaded_skills=["yaml"]) is False

    def test_json_skill_does_not_count_as_yaml_loaded(self) -> None:
        assert self._eval("/project/config/app.yaml", loaded_skills=["json"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/config/app.yaml", injected_skills=["yaml"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/config/app.yaml", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-plan-skill structure ---


class TestRequirePlanSkillStructure:
    """Verify require-plan-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-plan-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        for skill in (
            "gobby:references/plan/overview.md",
            "gobby:references/plan/drafting.md",
            "gobby:references/plan/review.md",
            "gobby:references/plan/enhancement.md",
        ):
            assert f"not skill_loaded('{skill}')" in body.when
        assert "event.data.get('canonical_tool_kind') == 'write'" in body.when
        assert "'.gobby/plans/' in" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-plan-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason is not None
        assert body.effects[0].reason.startswith(
            _skill_fetch_template("gobby:references/plan/overview.md")
        )

    @pytest.mark.asyncio
    async def test_installed_rule_evaluation_includes_current_plan_routing_guidance(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_file_path": "/project/.gobby/plans/design.md",
            },
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"loaded_skills": ["markdown"]},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert skill_fetch_directive("gobby:references/plan/overview.md") in response.reason
        assert "canonical file authority for dependent multi-deliverable plans" in response.reason
        assert "materialize the complete conversational draft" in response.reason
        assert "Atomic deliverables stay in the task workflow" in response.reason
        assert "registration waits for a real expansion root" in response.reason
        assert "Lightweight planning" not in response.reason
        assert "written by `gobby plans register`" not in response.reason


class TestCodeReviewFreshnessConditions:
    """The per-commit freshness ledger behind the code-review gate.

    The gate used to be satisfied-once: `not skill_loaded('code-review')` is
    false for the rest of a context epoch, so one `get_skill` bought unlimited
    ungated commits. These cases pin the four-rule cycle that replaced it.
    """

    RULES = "skill-discovery/require-code-review-skill.yaml"
    SKILL_GATE = _bundled_rule_condition(RULES, "require-code-review-skill")
    FRESHNESS_GATE = _bundled_rule_condition(RULES, "require-code-review-self-review")
    TRACK = _bundled_rule_condition(RULES, "track-ocr-review-freshness")
    CLEAR = _bundled_rule_condition(RULES, "clear-ocr-review-freshness")

    def _eval(
        self,
        condition: str,
        command: str,
        *,
        variables: dict[str, Any] | None = None,
        is_failure: bool | None = None,
        commit_has_reviewable_paths: bool = True,
        foreign_landing_merge: bool = False,
    ) -> bool:
        tool_input = {"command": command}
        context = {
            "variables": variables or {},
            "event": SimpleNamespace(
                data={"tool_name": "Bash", "tool_input": tool_input},
                metadata={} if is_failure is None else {"is_failure": is_failure},
            ),
            "tool_input": tool_input,
            "source": "interactive",
            "commit_has_reviewable_paths": commit_has_reviewable_paths,
            "foreign_landing_merge": foreign_landing_merge,
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return bool(evaluator.evaluate(condition))

    def test_skill_gate_covers_only_the_unloaded_state(self) -> None:
        assert self._eval(self.SKILL_GATE, "git commit -m x") is True
        loaded = {"loaded_skills": ["code-review"]}
        assert self._eval(self.SKILL_GATE, "git commit -m x", variables=loaded) is False

    def test_freshness_gate_blocks_a_later_commit_after_the_skill_loads(self) -> None:
        loaded = {"loaded_skills": ["code-review"]}
        assert self._eval(self.FRESHNESS_GATE, "git commit -m x", variables=loaded) is True

        reviewed = {"loaded_skills": ["code-review"], "code_review_fresh": True}
        assert self._eval(self.FRESHNESS_GATE, "git commit -m x", variables=reviewed) is False

        spent = {"loaded_skills": ["code-review"], "code_review_fresh": False}
        assert self._eval(self.FRESHNESS_GATE, "git commit -m x", variables=spent) is True

    def test_freshness_gate_defers_to_the_skill_gate(self) -> None:
        """Only one of the two blocks at a time, so the reason is never doubled."""
        assert self._eval(self.FRESHNESS_GATE, "git commit -m x") is False

    def test_neither_gate_covers_a_commit_with_nothing_reviewable(self) -> None:
        """A commit OCR excludes entirely has no review to demand."""
        assert (
            self._eval(self.SKILL_GATE, "git commit -m docs", commit_has_reviewable_paths=False)
            is False
        )
        loaded = {"loaded_skills": ["code-review"]}
        assert (
            self._eval(
                self.FRESHNESS_GATE,
                "git commit -m docs",
                variables=loaded,
                commit_has_reviewable_paths=False,
            )
            is False
        )

    def test_documentation_only_commit_still_spends_a_held_review(self) -> None:
        """The next code commit re-reviews; freshness is not saved by the skip."""
        assert self._eval(self.CLEAR, "git commit -m docs", is_failure=False) is True

    def test_review_grants_freshness_only_on_a_proven_success(self) -> None:
        rule_call = "ocr delegate rule --format json src/a.py"
        assert self._eval(self.TRACK, rule_call, is_failure=False) is True
        assert self._eval(self.TRACK, rule_call, is_failure=True) is False
        # An indeterminate outcome proves no review ran, so the gate stays shut.
        assert self._eval(self.TRACK, rule_call, is_failure=None) is False
        # preview alone lists paths and reviews nothing.
        assert self._eval(self.TRACK, "ocr delegate preview --format json", is_failure=False) is (
            False
        )

    def test_commit_spends_freshness_unless_it_demonstrably_failed(self) -> None:
        assert self._eval(self.CLEAR, "git commit -m x", is_failure=False) is True
        assert self._eval(self.CLEAR, "git commit -m x", is_failure=True) is False
        # Unknown outcome may have committed, so the review is spent. The
        # opposite default would leave a stale review valid on any provider
        # that cannot report a Bash exit code.
        assert self._eval(self.CLEAR, "git commit -m x", is_failure=None) is True
        assert self._eval(self.CLEAR, "git log --oneline", is_failure=False) is False

    @pytest.mark.parametrize(
        "command",
        ["git merge --no-ff task-1", "git cherry-pick abc1234", "git revert --no-edit abc1234"],
    )
    def test_authored_operations_spend_freshness(self, command: str) -> None:
        assert self._eval(self.CLEAR, command, is_failure=False) is True

    def test_foreign_landing_merge_preserves_freshness(self) -> None:
        assert (
            self._eval(
                self.CLEAR,
                "git merge --no-ff task-1",
                is_failure=False,
                foreign_landing_merge=True,
            )
            is False
        )


class TestRequirePlanSkillCondition:
    """Test the require-plan-skill condition evaluates correctly."""

    CONDITION = _bundled_rule_condition(
        "skill-discovery/require-plan-skill.yaml", "require-plan-skill"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skill_references": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.gobby/plans/wiki-output-design.md",
            "/project/.gobby/plans/coverage/wiki-output-design.coverage.yaml",
            "/project/.gobby/plans/completed/coderabbit-fixes.md",
            ".gobby/plans/wiki-output-design.md",
            "C:\\project\\.gobby\\plans\\wiki-output-design.md",
        ],
    )
    def test_matches_plan_artifact_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/docs/plans/roadmap.md",
            "/project/plans/roadmap.md",
            "/project/.gobby/tasks/backlog.md",
            "/project/.gobby/project.json",
            "/project/.claude/plans/19670-scratch.md",
            "/project/src/gobby/plans/service.py",
        ],
    )
    def test_skips_non_plan_artifact_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    @pytest.mark.parametrize(
        "skill",
        [
            "gobby:references/plan/overview.md",
            "gobby:references/plan/drafting.md",
            "gobby:references/plan/review.md",
            "gobby:references/plan/enhancement.md",
        ],
    )
    def test_skips_when_any_plan_family_skill_loaded(self, skill: str) -> None:
        assert self._eval("/project/.gobby/plans/design.md", loaded_skills=[skill]) is False

    def test_unrelated_skill_does_not_count_as_plan_loaded(self) -> None:
        assert self._eval("/project/.gobby/plans/design.md", loaded_skills=["yaml"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert (
            self._eval(
                "/project/.gobby/plans/design.md",
                injected_skills=["gobby:references/plan/overview.md"],
            )
            is True
        )

    def test_skips_reads_of_plan_artifacts(self) -> None:
        assert self._eval("/project/.gobby/plans/design.md", canonical_tool_kind="read") is False

    def test_skips_searches_over_plan_artifacts(self) -> None:
        assert self._eval("/project/.gobby/plans/design.md", canonical_tool_kind="search") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-json-skill structure ---


class TestRequireJsonSkillStructure:
    """Verify require-json-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-json-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('json')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-json-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("json")


class TestRequireJsonSkillCondition:
    """Test the require-json-skill condition evaluates correctly."""

    CONDITION = _bundled_rule_condition(
        "skill-discovery/require-json-skill.yaml",
        "require-json-skill",
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        content_write: bool = True,
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        event_data = {
            "canonical_tool_kind": canonical_tool_kind,
            "canonical_file_path": file_path,
        }
        if content_write:
            event_data["canonical_write_file_path"] = file_path
        context = {
            "variables": variables,
            "event": SimpleNamespace(data=event_data),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package.json",
            "/project/package-lock.json",
            "/project/tsconfig.json",
            "/project/tsconfig.build.json",
            "/project/schema/app-config.schema.json",
            "/project/config/app.jsonc",
            "/project/config/app.json5",
            "/project/tests/fixtures/api-response.json",
        ],
    )
    def test_matches_json_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.babelrc",
            "/project/.eslintrc",
            "/project/.firebaserc",
            "/project/.hintrc",
            "/project/.prettierrc",
            "/project/.stylelintrc",
            "/project/.swcrc",
        ],
    )
    def test_matches_extensionless_json_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/config/app.json.bak",
            "/project/config/app.json.disabled",
            "/project/config/app.yaml",
            "/project/config/app.toml",
            "/project/.prettierignore",
            "/project/src/main.ts",
            "/project/src/App.tsx",
        ],
    )
    def test_skips_non_json_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/package.json", loaded_skills=["json"]) is False

    def test_yaml_skill_does_not_count_as_json_loaded(self) -> None:
        assert self._eval("/project/package.json", loaded_skills=["yaml"]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/package.json", injected_skills=["json"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/package.json", canonical_tool_kind="read") is False

    def test_skips_non_authoring_mutation(self) -> None:
        assert self._eval("/project/package.json", content_write=False) is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-php-skill structure ---


class TestRequirePhpSkillStructure:
    """Verify require-php-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-php-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('php')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-php-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("php")


# --- require-php-skill condition evaluation ---


class TestRequirePhpSkillCondition:
    """Test the require-php-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('php') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith('.php') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('composer.json', 'composer.lock', 'symfony.lock', 'phpunit.xml', "
        "'phpunit.xml.dist', 'pest.php', 'phpstan.neon', 'phpstan.neon.dist', "
        "'psalm.xml', 'psalm.xml.dist') "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] "
        "in ('rector.php', 'ecs.php', 'pint.json', 'phpcs.xml', "
        "'phpcs.xml.dist', 'infection.json', '.php-cs-fixer.php', "
        "'.php-cs-fixer.dist.php')"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/Controller/AccountController.php",
            "/project/tests/Account/AccountTest.php",
            "/project/config/packages/security.php",
            "/project/.php-cs-fixer.php",
        ],
    )
    def test_matches_php_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/composer.json",
            "/project/composer.lock",
            "/project/symfony.lock",
            "/project/phpunit.xml",
            "/project/phpunit.xml.dist",
            "/project/pest.php",
            "/project/phpstan.neon",
            "/project/phpstan.neon.dist",
            "/project/psalm.xml",
            "/project/psalm.xml.dist",
            "/project/rector.php",
            "/project/ecs.php",
            "/project/pint.json",
            "/project/phpcs.xml",
            "/project/phpcs.xml.dist",
            "/project/infection.json",
            "/project/.php-cs-fixer.dist.php",
        ],
    )
    def test_matches_php_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package.json",
            "/project/composer.json.bak",
            "/project/phpstan.neon.bak",
            "/project/src/Account.ts",
            "/project/templates/account.twig",
            "/project/config/services.yaml",
        ],
    )
    def test_skips_non_php_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/Controller/Main.php", loaded_skills=["php"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/Controller/Main.php", injected_skills=["php"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/Controller/Main.php", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-typescript-skill structure ---


class TestRequireTypeScriptSkillStructure:
    """Verify require-typescript-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-typescript-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('typescript')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-typescript-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("typescript")


# --- require-typescript-skill condition evaluation ---


class TestRequireTypeScriptSkillCondition:
    """Test the require-typescript-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('typescript') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(('.ts', '.tsx', '.mts', '.cts')) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] == 'tsconfig.json' "
        "or ("
        "event.data.get('canonical_file_path', '').rpartition('/')[2].startswith('tsconfig.') "
        "and event.data.get('canonical_file_path', '').rpartition('/')[2].endswith('.json')"
        ")"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/src/main.ts",
            "/project/src/component.tsx",
            "/project/src/entry.mts",
            "/project/src/legacy.cts",
            "/project/types/global.d.ts",
        ],
    )
    def test_matches_typescript_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/tsconfig.json",
            "/project/tsconfig.build.json",
            "/project/packages/client/tsconfig.esm.json",
        ],
    )
    def test_matches_tsconfig_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/package.json",
            "/project/src/main.js",
            "/project/src/main.jsx",
            "/project/my-tsconfig.json",
            "/project/tsconfig.json5",
        ],
    )
    def test_skips_non_typescript_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.ts", loaded_skills=["typescript"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.ts", injected_skills=["typescript"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main.ts", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-impeccable-skill structure ---


class TestRequireImpeccableSkillStructure:
    """Verify require-impeccable-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-impeccable-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('impeccable')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-impeccable-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("impeccable")


# --- shared UI-file predicate (require-impeccable-skill + design detector) ---


UI_PREDICATE_MATCHING_PATHS = [
    "/project/src/components/Button.tsx",
    "/project/src/legacy/Widget.jsx",
    "/project/src/App.vue",
    "/project/src/Card.svelte",
    "/project/src/page.astro",
    "/project/styles/theme.css",
    "/project/styles/mixins.scss",
    "/project/public/index.html",
    "web/src/lib/api.ts",
    "web/src/util/format.js",
    "/Users/dev/repo/web/src/lib/api.ts",
    "/Users/dev/repo/web/scripts/build.mjs",
]

UI_PREDICATE_NON_MATCHING_PATHS = [
    "src/gobby/install/shared/skills/impeccable/scripts/live-copy-edit-agent.mjs",
    "/Users/dev/repo/src/gobby/install/shared/skills/impeccable/scripts/hook.mjs",
    "/project/scripts/release.mjs",
    "/project/eslint.config.js",
    "/project/src/daemon/main.ts",
    "/project/src/gobby/servers/http.py",
    "/project/README.md",
]


def _eval_condition(condition: str, file_path: str, **variables: Any) -> bool:
    context = {
        "variables": {"loaded_skills": [], **variables},
        "event": SimpleNamespace(
            data={
                "canonical_tool_kind": "write",
                "canonical_file_path": file_path,
            }
        ),
        "tool_input": {},
    }
    allowed_funcs = build_condition_helpers(context=context)
    evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
    return evaluator.evaluate(condition)


class TestRequireImpeccableSkillCondition:
    """Test the require-impeccable-skill condition from the bundled template."""

    @pytest.fixture
    def condition(self) -> str:
        return _bundled_rule_condition(
            "skill-discovery/require-impeccable-skill.yaml", "require-impeccable-skill"
        )

    @pytest.mark.parametrize("file_path", UI_PREDICATE_MATCHING_PATHS)
    def test_matches_ui_writes(self, condition: str, file_path: str) -> None:
        assert _eval_condition(condition, file_path) is True

    @pytest.mark.parametrize("file_path", UI_PREDICATE_NON_MATCHING_PATHS)
    def test_skips_non_ui_writes(self, condition: str, file_path: str) -> None:
        assert _eval_condition(condition, file_path) is False

    def test_skips_when_already_loaded(self, condition: str) -> None:
        assert (
            _eval_condition(condition, "/project/src/Button.tsx", loaded_skills=["impeccable"])
            is False
        )

    def test_matches_ui_path_in_canonical_file_paths(self, condition: str) -> None:
        context = {
            "variables": {"loaded_skills": []},
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": "write",
                    "canonical_file_paths": [None, "README.md", "web/src/lib/api.ts"],
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        assert evaluator.evaluate(condition) is True


class TestDesignDetectorPredicate:
    """The design detector's edit pass shares the UI-file predicate."""

    @pytest.fixture
    def condition(self) -> str:
        return _bundled_rule_condition("impeccable/design-detector.yaml", "impeccable-edit-pass")

    @pytest.mark.parametrize("file_path", UI_PREDICATE_MATCHING_PATHS)
    def test_matches_ui_writes(self, condition: str, file_path: str) -> None:
        assert _eval_condition(condition, file_path) is True

    @pytest.mark.parametrize("file_path", UI_PREDICATE_NON_MATCHING_PATHS)
    def test_skips_non_ui_writes(self, condition: str, file_path: str) -> None:
        assert _eval_condition(condition, file_path) is False


# --- require-bash-skill structure ---


class TestRequireBashSkillStructure:
    """Verify require-bash-skill rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-bash-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('bash')" in body.when

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-bash-skill")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert len(body.resolved_effects) == 1
        assert body.resolved_effects[0].type == "block"
        assert body.resolved_effects[0].reason == _skill_fetch_template("bash")


# --- require-bash-skill condition evaluation ---


class TestRequireBashSkillCondition:
    """Test the require-bash-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('bash') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and ("
        "event.data.get('canonical_file_path', '').endswith(("
        "'.sh', '.bash', '.bats', "
        "'.sh.j2', '.bash.j2', '.sh.tpl', '.bash.tpl', "
        "'.sh.tmpl', '.bash.tmpl', '.sh.template', '.bash.template'"
        ")) "
        "or event.data.get('canonical_file_path', '').rpartition('/')[2] in ("
        "'.bashrc', '.bash_profile', '.bash_login', '.bash_logout', "
        "'.bash_aliases', '.bash_completion', '.shellcheckrc', 'Bashfile', 'PKGBUILD'"
        ")"
        ")"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/scripts/deploy.sh",
            "/project/scripts/setup.bash",
            "/project/tests/deploy.bats",
            "/project/templates/deploy.sh.j2",
            "/project/templates/setup.bash.tmpl",
        ],
    )
    def test_matches_bash_source_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/.bashrc",
            "/project/.bash_profile",
            "/project/.bash_aliases",
            "/project/.shellcheckrc",
            "/project/Bashfile",
            "/project/packages/arch/PKGBUILD",
        ],
    )
    def test_matches_bash_config_writes(self, file_path: str) -> None:
        assert self._eval(file_path) is True

    @pytest.mark.parametrize(
        "file_path",
        [
            "/project/scripts/deploy.zsh",
            "/project/scripts/deploy.fish",
            "/project/scripts/deploy.sh.txt",
            "/project/.profile",
            "/project/Makefile",
            "/project/Dockerfile",
        ],
    )
    def test_skips_non_bash_targets(self, file_path: str) -> None:
        assert self._eval(file_path) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/scripts/deploy.sh", loaded_skills=["bash"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/scripts/deploy.sh", injected_skills=["bash"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/scripts/deploy.sh", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


class TestCodeIndexRuleCondition:
    """Test the code-index onboarding rule against canonical tool metadata."""

    CONDITION = (
        "variables.get('code_index_available') "
        "and not skill_loaded('gobby:references/code-index/overview.md') "
        'and not has_open_tool_error("gobby-skills/get_skill_file", {"name": "gobby", "path": "references/code-index/overview.md"}) '
        "and not variables.get('code_index_preflight_warning') "
        "and not event.data.get('canonical_code_index_navigation') "
        "and event.data.get('canonical_code_navigation_broad') "
        "and event.data.get('canonical_code_navigation_repo_scope') is not False"
    )

    def _eval(
        self,
        *,
        code_index_available: bool = True,
        canonical_code_navigation_broad: bool = True,
        canonical_code_index_navigation: bool = False,
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
        code_index_preflight_warning: bool = False,
        open_tool_errors: Any = None,
    ) -> bool:
        variables = {
            "loaded_skill_references": loaded_skills or [],
            "code_index_available": code_index_available,
        }
        if open_tool_errors is not None:
            variables["open_tool_errors"] = open_tool_errors
        if code_index_preflight_warning:
            variables["code_index_preflight_warning"] = {
                "preflight": "code_index",
                "message": "gcode_index_unavailable",
            }
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_code_navigation_broad": canonical_code_navigation_broad,
                    "canonical_code_index_navigation": canonical_code_index_navigation,
                    "canonical_code_navigation_repo_scope": True,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_broad_code_navigation(self) -> None:
        assert self._eval(canonical_code_navigation_broad=True) is True

    def test_matches_search(self) -> None:
        assert self._eval(canonical_code_navigation_broad=True) is True

    def test_skips_narrow_context_read(self) -> None:
        assert self._eval(canonical_code_navigation_broad=False) is False

    def test_skips_when_code_index_unavailable(self) -> None:
        assert self._eval(code_index_available=False) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval(loaded_skills=["gobby:references/code-index/overview.md"]) is False

    def test_skips_for_exact_code_index_skill_load_error(self) -> None:
        assert (
            self._eval(
                open_tool_errors=[
                    _skill_tool_error_record("gobby:references/code-index/overview.md")
                ]
            )
            is False
        )

    def test_does_not_skip_for_unrelated_skill_load_error(self) -> None:
        assert self._eval(open_tool_errors=[_skill_tool_error_record("brevity")]) is True

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval(injected_skills=["code-index"]) is True

    def test_skips_when_isolated_code_index_preflight_failed(self) -> None:
        assert self._eval(code_index_preflight_warning=True) is False

    def test_skips_gcode_navigation(self) -> None:
        assert self._eval(canonical_code_index_navigation=True) is False


class TestRequireCodeIndexSkillStructure:
    """Verify require-code-index-skill blocks with the canonical directive."""

    def test_has_block_effect_with_canonical_directive(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-code-index-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "variables.get('code_index_available')" in body.when
        assert "not skill_loaded('gobby:references/code-index/overview.md')" in body.when
        assert "not has_open_tool_error(" in body.when
        assert '"gobby-skills/get_skill_file"' in body.when
        assert '"path": "references/code-index/overview.md"' in body.when
        assert "not variables.get('code_index_preflight_warning')" in body.when
        assert body.effects is not None
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        reason = body.effects[0].reason
        assert reason is not None
        assert _skill_fetch_template("gobby:references/code-index/overview.md") in reason
        assert "If that call fails, its recorded failure fails this rule open" in reason
        assert '`gcode search-symbol "name"`' in reason
        assert '`gcode grep -w "identifier" -m 50`' in reason
        assert '`gcode grep -F "literal" -m 50`' in reason
        assert '`gcode search-content "text"`' in reason
        assert '`gcode search "concept"`' in reason
        assert "switch lanes" in reason
        assert "list_tools" not in reason

    def test_code_index_navigation_rules_sync(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        expected = {
            "reset-code-index-navigation",
            "track-code-index-attempts",
            "track-code-index-navigation",
            "track-gcode-fail-open",
            "track-turn-written-paths",
            "note-code-index-preflight-fail-open",
            "prefer-gcode-for-source-read",
        }
        rules = {row.name for row in manager.list_all()}
        assert expected.issubset(rules)

    def test_code_index_block_rules_are_repo_scoped(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        for rule_name in ("require-code-index-skill", "prefer-gcode-for-source-read"):
            row = manager.get_by_name(rule_name)
            assert row is not None
            body = RuleDefinitionBody.model_validate(row.definition_json)
            assert body.when is not None
            assert "navigation_requires_index(event.data" in body.when
            assert "not variables.get('code_index_preflight_warning')" in body.when

    def test_code_index_recovery_allowlist_names_installed_rules(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        rules = {row.name for row in manager.list_all()}

        assert _CODE_INDEX_REMEDIATION_RULES.issubset(rules)


class TestCodeIndexNavigationRules:
    """Verify indexed-project gcode-first rule behavior."""

    @staticmethod
    def _variables(*, loaded: bool = False, used: bool = False) -> dict[str, Any]:
        return {
            "loaded_skill_references": ["gobby:references/code-index/overview.md"]
            if loaded
            else [],
            "code_index_available": True,
            "code_index_navigation_used_this_turn": used,
            "turn_written_paths": [],
            "brevity_disabled": True,
            "skill_discovery_instructions_shown": True,
        }

    @staticmethod
    def _event(event_type: HookEventType, data: dict[str, Any]) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=data,
        )

    @classmethod
    def _normalized_bash_event(
        cls,
        command: str,
        *,
        cwd: str | None = None,
        project_path: str | None = None,
    ) -> HookEvent:
        data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}
        if cwd is not None:
            data["cwd"] = cwd
        if project_path is not None:
            data["project_path"] = project_path
        normalize_tool_fields(data)
        return cls._event(HookEventType.BEFORE_TOOL, data)

    @staticmethod
    def _linked_checkouts(tmp_path: Path) -> tuple[Path, Path]:
        primary = tmp_path / "primary"
        linked = tmp_path / "linked"
        common_dir = primary / ".git"
        linked_git_dir = common_dir / "worktrees" / "linked"
        linked_git_dir.mkdir(parents=True)
        (primary / "src").mkdir()
        (linked / "src").mkdir(parents=True)
        (linked / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
        (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
        return primary, linked

    @pytest.mark.parametrize("reset_source", ["clear", "compact"])
    @pytest.mark.asyncio
    async def test_guidance_once_per_context_epoch(
        self, db: HubDatabase, reset_source: str
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._variables()
        commands = (
            "rg pattern src",
            "grep -R pattern src",
            "sed -n '1,5p' src/app.py",
            "awk 'NR < 5' src/app.py",
        )
        first = await engine.evaluate(
            self._normalized_bash_event(commands[0]), session_id=SESSION_ID, variables=variables
        )
        assert first.decision == "block"
        assert skill_fetch_directive("gobby:references/code-index/overview.md") in (
            first.reason or ""
        )
        # The completed reference loader records this ledger; partial loads do not.
        variables["loaded_skill_references"] = ["gobby:references/code-index/overview.md"]
        for _ in range(2):
            await engine.evaluate(
                self._event(HookEventType.BEFORE_AGENT, {"prompt": "continue"}),
                session_id=SESSION_ID,
                variables=variables,
            )
            for command in commands:
                response = await engine.evaluate(
                    self._normalized_bash_event(command), session_id=SESSION_ID, variables=variables
                )
                assert response.decision == "allow", command
        await engine.evaluate(
            self._event(HookEventType.SESSION_START, {"source": reset_source}),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert variables["loaded_skill_references"] == []
        again = await engine.evaluate(
            self._normalized_bash_event(commands[0]), session_id=SESSION_ID, variables=variables
        )
        assert again.decision == "block"

    @pytest.mark.parametrize("command", ["printf 'grep pattern src'", "cp grep.txt copy.txt"])
    @pytest.mark.asyncio
    async def test_search_word_in_unrelated_command_does_not_teach(
        self, db: HubDatabase, command: str
    ) -> None:
        _sync_bundled(db)
        response = await RuleEngine(db).evaluate(
            self._normalized_bash_event(command), session_id=SESSION_ID, variables=self._variables()
        )
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_first_rg_requires_code_index_skill(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "rg pattern src",
                "canonical_tool_kind": "search",
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "block"
        assert response.reason is not None
        assert skill_fetch_directive("gobby:references/code-index/overview.md") in response.reason
        assert "If that call fails, its recorded failure fails this rule open" in response.reason

    @pytest.mark.asyncio
    async def test_searchable_project_text_redirects_to_gcode_content_search(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        searchable_text = (
            "grep -n '^#' .gobby/plans/herdr-terminal-client.md",
            "grep needle docs/research/agent-feedback-loops.md",
        )

        for loaded in (False, True):
            variables = self._variables(loaded=loaded)
            for command in searchable_text:
                response = await engine.evaluate(
                    self._normalized_bash_event(command),
                    session_id=SESSION_ID,
                    variables=variables,
                )
                assert response.decision == ("allow" if loaded else "block"), (loaded, command)

            shell_output_filter = await engine.evaluate(
                self._normalized_bash_event("git log --oneline | grep handoff"),
                session_id=SESSION_ID,
                variables=variables,
            )
            assert shell_output_filter.decision == "allow", loaded

    @pytest.mark.asyncio
    async def test_code_index_skill_proxy_error_fails_open_until_matching_success(
        self,
        db: HubDatabase,
    ) -> None:
        _sync_bundled(db)
        project_id = "22222222-2222-4222-8222-222222222222"
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, "code-index-fail-open"),
        )
        session_id = SessionManager(db).register_session(
            external_id="code-index-fail-open-session",
            # None resolves the current machine; a hardcoded id is rejected as
            # foreign by machine-scoped ownership.
            machine_id=None,
            source="codex",
            project_id=project_id,
            project_path="/tmp/code-index-fail-open",
        )
        assert session_id
        state_manager = SessionVariableManager(db)
        code_index_identity = (
            "gobby-skills",
            "get_skill_file",
            {"name": "gobby", "path": "references/code-index/overview.md"},
        )
        track_proxy_outcome(
            state_manager,
            session_id,
            code_index_identity,
            code_index_identity,
            {"success": False, "error": "Workflow evaluation timed out after 15s"},
            "failed_pre_dispatch",
        )
        variables = self._variables(loaded=False)
        variables["open_tool_errors"] = state_manager.get_variables(session_id)["open_tool_errors"]
        engine = RuleEngine(db)

        for command in (
            "rg pattern src",
            "grep -R pattern src",
            'gcode grep "pattern" src || true',
        ):
            event = self._normalized_bash_event(command)
            assert event.data["canonical_code_navigation_broad"] is True
            if command.startswith("gcode"):
                assert "canonical_code_index_navigation" not in event.data
            response = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=variables,
            )
            assert response.decision == "allow"
        # The gcode call returned matches, so its attempt no longer holds search open.
        gcode_outcome = self._normalized_bash_event('gcode grep "pattern" src || true')
        gcode_outcome.data["tool_output"] = "src/app.py:1:pattern"
        normalize_tool_fields(gcode_outcome.data)
        gcode_outcome.event_type = HookEventType.AFTER_TOOL
        await engine.evaluate(gcode_outcome, session_id=SESSION_ID, variables=variables)

        track_proxy_outcome(
            state_manager,
            session_id,
            code_index_identity,
            code_index_identity,
            {
                "success": True,
                "file": {"skill_name": "gobby", "path": "references/code-index/overview.md"},
            },
            "executed",
        )
        variables["open_tool_errors"] = state_manager.get_variables(session_id)["open_tool_errors"]
        assert variables["loaded_skill_references"] == []
        assert variables["open_tool_errors"] == []

        retry = await engine.evaluate(
            self._normalized_bash_event("rg pattern src"),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert retry.decision == "block"

        unrelated_identity = (
            "gobby-skills",
            "get_skill",
            {"name": "brevity"},
        )
        track_proxy_outcome(
            state_manager,
            session_id,
            unrelated_identity,
            unrelated_identity,
            {"success": False, "error": "proxy unavailable"},
            "failed_pre_dispatch",
        )
        variables["open_tool_errors"] = state_manager.get_variables(session_id)["open_tool_errors"]

        unrelated = await engine.evaluate(
            self._normalized_bash_event("rg pattern src"),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert unrelated.decision == "block"

    @pytest.mark.asyncio
    async def test_loaded_code_index_allows_raw_search_without_failure(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "rg pattern src",
                "canonical_tool_kind": "search",
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=True),
        )

        assert response.decision == "allow"

    @pytest.mark.parametrize(
        ("code_index_available", "warning_message"),
        [
            pytest.param(False, "gcode_index_unavailable", id="unavailable"),
            pytest.param(True, "gcode_index_stale", id="stale"),
        ],
    )
    @pytest.mark.asyncio
    async def test_preflight_unavailable_or_stale_allows_raw_search_with_note(
        self,
        db: HubDatabase,
        tmp_path: Path,
        code_index_available: bool,
        warning_message: str,
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        repo.mkdir()
        variables = self._variables(loaded=False)
        variables["code_index_available"] = code_index_available
        variables["code_index_preflight_warning"] = {
            "preflight": "code_index",
            "message": warning_message,
        }

        response = await RuleEngine(db).evaluate(
            self._normalized_bash_event(
                "rg pattern src",
                cwd=str(repo),
                project_path=str(repo),
            ),
            session_id=SESSION_ID,
            variables=variables,
        )

        assert response.decision == "allow"
        assert "preflight reported this checkout unavailable or stale" in (response.context or "")
        assert variables["code_index_preflight_note_shown"] is True

    @pytest.mark.asyncio
    async def test_log_search_bypasses_code_index_rules(
        self, db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        gobby_home = tmp_path / "gobby-home"
        log_path = gobby_home / "logs" / "daemon.log"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

        for loaded in (False, True):
            event = self._normalized_bash_event(
                f"rg error {log_path}",
                cwd=str(repo),
                project_path=str(repo),
            )
            assert event.data["canonical_code_navigation_repo_scope"] is False

            response = await RuleEngine(db).evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )

            assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_session_scratchpad_search_is_explicitly_allowed(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo, _ = self._linked_checkouts(tmp_path)
        scratch_root = tmp_path / "scratchpad"
        scratch_git_dir = repo / ".git" / "worktrees" / "scratch"
        scratch_git_dir.mkdir()
        (scratch_root / "src").mkdir(parents=True)
        (scratch_root / ".git").write_text(f"gitdir: {scratch_git_dir}\n", encoding="utf-8")
        (scratch_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
        scratchpad = scratch_root / "src" / "session.log"

        event = self._normalized_bash_event(
            f"rg conflict {scratchpad}",
            cwd=str(repo),
            project_path=str(repo),
        )
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_code_navigation_repo_scope"] is False
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_foreign_repo_search_is_explicitly_allowed(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        foreign = tmp_path / "foreign"
        (repo / ".git").mkdir(parents=True)
        (foreign / ".git").mkdir(parents=True)
        (foreign / "src").mkdir()

        event = self._normalized_bash_event(
            f"rg conflict {foreign / 'src'}",
            cwd=str(repo),
            project_path=str(repo),
        )
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_code_navigation_repo_scope"] is False
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_linked_worktree_conflict_search_redirects_to_gcode_grep(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        primary, linked = self._linked_checkouts(tmp_path)

        event = self._normalized_bash_event(
            f"rg conflict {linked / 'src'}",
            cwd=str(primary),
            project_path=str(primary),
        )
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_code_navigation_repo_scope"] is True
        assert response.decision == "block"
        assert 'gcode grep -F "literal"' in (response.reason or "")

    @pytest.mark.asyncio
    async def test_pathless_search_in_linked_worktree_defaults_scope_to_cwd(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        primary, linked = self._linked_checkouts(tmp_path)

        event = self._normalized_bash_event(
            "rg conflict",
            cwd=str(linked),
            project_path=str(primary),
        )
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data.get("canonical_file_paths", []) == []
        assert event.data["canonical_code_navigation_repo_scope"] is True
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_primary_checkout_search_from_worktree_redirects_to_gcode_grep(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        primary, linked = self._linked_checkouts(tmp_path)

        event = self._normalized_bash_event(
            f"grep -R conflict {primary / 'src'}",
            cwd=str(linked),
            project_path=str(linked),
        )
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_code_navigation_repo_scope"] is True
        assert response.decision == "block"
        assert 'gcode grep -F "literal"' in (response.reason or "")

    @pytest.mark.asyncio
    async def test_mixed_scratchpad_and_indexed_paths_redirects_for_indexed_path(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        primary, linked = self._linked_checkouts(tmp_path)
        scratchpad = tmp_path / "scratchpad" / "session.log"

        event = self._normalized_bash_event(
            f"rg conflict {scratchpad} {linked / 'src'}",
            cwd=str(primary),
            project_path=str(primary),
        )
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_code_navigation_repo_scope"] is True
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_unexpanded_home_search_bypasses_code_index_rule(
        self, db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sync_bundled(db)
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        monkeypatch.setenv("HOME", str(home))

        cases = (
            ('rg -l "pat" "$HOME/Library/Application Support/rtk/tee"', False, "allow"),
            ("rg foo src", True, "block"),
        )
        for command, expected_repo_scope, expected_decision in cases:
            event = self._normalized_bash_event(
                command,
                cwd=str(repo),
                project_path=str(repo),
            )
            assert event.data["canonical_code_navigation_repo_scope"] is expected_repo_scope

            response = await RuleEngine(db).evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=False),
            )

            assert response.decision == expected_decision

    @pytest.mark.asyncio
    async def test_file_discovery_scope_controls_code_index_rules(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        extraction = tmp_path / "workbook-extract"
        cases = (
            ("rg --files", extraction, False, "allow"),
            (f"rg --files {extraction}", repo, False, "allow"),
            (f"rg --files {extraction} | rg '(parser|language).*test'", repo, False, "allow"),
            (f"rg --files {extraction} src", repo, True, "block"),
            (f"find {extraction} -type f", repo, False, "allow"),
            ("rg pattern src", repo, True, "block"),
        )

        for command, cwd, expected_repo_scope, expected_decision in cases:
            event = self._normalized_bash_event(
                command,
                cwd=str(cwd),
                project_path=str(repo),
            )
            assert event.data["canonical_code_navigation_repo_scope"] is expected_repo_scope

            response = await RuleEngine(db).evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=False),
            )

            assert response.decision == expected_decision

    @pytest.mark.parametrize(
        "command",
        [
            "find . -maxdepth 1",
            "find docs -type d",
            "find crates -path '*migrations*' -name '*.sql'",
        ],
    )
    @pytest.mark.asyncio
    async def test_find_navigation_redirects_to_gcode_tree(
        self, db: HubDatabase, command: str
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = self._normalized_bash_event(command)

        assert event.data["canonical_tool_kind"] == "execute"
        assert event.data["canonical_code_navigation_action"] == "enumerate"
        assert event.data["canonical_code_navigation_broad"] is True
        assert event.data["canonical_code_navigation_gcode_supported"] is True
        for loaded in (False, True):
            response = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )
            assert response.decision == ("allow" if loaded else "block"), (loaded, command)
            if not loaded:
                assert "gcode" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_find_newer_filesystem_query_is_explicitly_allowed(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = self._normalized_bash_event("find crates -newer Cargo.toml -name '*.rs'")

        assert event.data["canonical_code_navigation_action"] == "enumerate"
        assert event.data["canonical_code_navigation_gcode_supported"] is False
        for loaded in (False, True):
            response = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )
            assert response.decision == "allow", loaded

    @pytest.mark.parametrize(
        "command",
        [
            "git grep -n P 0.5.0 -- src/",
            "git grep -n P HEAD~1",
        ],
    )
    @pytest.mark.asyncio
    async def test_revision_scoped_git_grep_bypasses_code_index_rules(
        self, db: HubDatabase, command: str
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = self._normalized_bash_event(command)

        assert event.data["canonical_search_revision_scoped"] is True
        for loaded in (False, True):
            response = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )
            assert response.decision == "allow", (loaded, command)

    @pytest.mark.asyncio
    async def test_worktree_git_grep_still_uses_code_index_rules(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = self._normalized_bash_event("git grep -n P -- src/")

        assert "canonical_search_revision_scoped" not in event.data
        for loaded in (False, True):
            response = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )
            assert response.decision == ("allow" if loaded else "block"), loaded

    @pytest.mark.parametrize(
        "command",
        [
            "rg foo",
            "grep -rn foo src/",
            "grep -rn foo docs/",
            "git grep -n foo",
        ],
    )
    @pytest.mark.asyncio
    async def test_ordinary_repo_searches_still_use_code_index_rules(
        self, db: HubDatabase, command: str
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = self._normalized_bash_event(command)

        for loaded in (False, True):
            response = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )
            assert response.decision == ("allow" if loaded else "block"), (loaded, command)

    @pytest.mark.asyncio
    async def test_current_byte_verification_of_same_turn_write_is_explicitly_allowed(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._variables(loaded=False)
        write = self._event(
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "src/app.py"},
                "canonical_tool_kind": "write",
                "canonical_file_paths": ["src/app.py"],
            },
        )

        await engine.evaluate(write, session_id=SESSION_ID, variables=variables)

        assert variables["turn_written_paths"] == ["src/app.py"]
        for command in ("grep -n X src/app.py", "cat src/app.py"):
            event = self._normalized_bash_event(command)
            allowed = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=variables,
            )
            blocked = await engine.evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=False),
            )

            assert allowed.decision == "allow", command
            assert blocked.decision == "block", command

    @pytest.mark.asyncio
    async def test_gcode_fail_open_allows_fallback_search(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        variables["code_index_recoveries"] = [{"action": "search", "paths": ["src"]}]
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "rg pattern src",
                "canonical_tool_kind": "search",
                "canonical_file_paths": ["src"],
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "gcode grep -F 'gcode-runtime' -m 50",
            "gcode --project /external/archive tree core",
            "gcode --format=json --project=/external/archive tree core",
        ],
    )
    async def test_codex_gcode_error_allows_raw_retry_but_ordinary_search_blocks(
        self,
        db: HubDatabase,
        command: str,
        tmp_path: Path,
    ) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        completed_item = {
            "id": "gcode-error",
            "type": "dynamicToolCall",
            "namespace": "functions",
            "tool": "exec",
            "arguments": (
                f"const r = await tools.exec_command({{cmd: {json.dumps(command)}}}); "
                "text(r.output);"
            ),
            "contentItems": [
                {
                    "type": "inputText",
                    "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                },
                {
                    "type": "inputText",
                    "text": ('{"error":"io","message":"grant io error: Operation not permitted"}'),
                },
            ],
            "status": "completed",
            "success": None,
        }
        failure_data = CodexAdapter()._build_completed_tool_data(completed_item)
        failure_data.update(cwd=str(tmp_path), project_path=str(tmp_path))
        normalize_tool_fields(failure_data)

        assert failure_data["canonical_code_index_navigation"] is True
        assert failure_data["canonical_code_index_recovery"][0]["action"] == "outage"
        await RuleEngine(db).evaluate(
            self._event(HookEventType.AFTER_TOOL, failure_data),
            session_id=SESSION_ID,
            variables=variables,
        )

        raw_retry = self._normalized_bash_event(
            "rg -n -F 'gcode-runtime' .", cwd=str(tmp_path), project_path=str(tmp_path)
        )
        retry_response = await RuleEngine(db).evaluate(
            raw_retry,
            session_id=SESSION_ID,
            variables=variables,
        )
        ordinary_response = await RuleEngine(db).evaluate(
            raw_retry,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert variables["code_index_recoveries"]
        assert retry_response.decision == ("block" if "--project" in command else "allow")
        assert ordinary_response.decision == "block"

    @pytest.mark.asyncio
    async def test_gcode_fail_open_bypasses_skill_requirement(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        variables["code_index_recoveries"] = [{"action": "search", "paths": ["src"]}]
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "rg pattern src",
                "canonical_tool_kind": "search",
                "canonical_file_paths": ["src"],
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_gcode_prefixed_compound_read_retains_enforcement(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._normalized_bash_event(
            "gcode outline src/a.py --format text; cat src/a.py",
            cwd=str(repo),
            project_path=str(repo),
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_zero_symbol_recovery_retains_path_and_operation_gates(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        outcome = self._normalized_bash_event(
            "gcode outline src/constants.py", cwd=str(tmp_path), project_path=str(tmp_path)
        )
        outcome.data["tool_output"] = (
            "file has no indexed symbols in current project: src/constants.py"
        )
        normalize_tool_fields(outcome.data)
        outcome.event_type = HookEventType.AFTER_TOOL
        engine = RuleEngine(db)
        await engine.evaluate(outcome, session_id=SESSION_ID, variables=variables)
        for command, decision in (
            ("cat src/constants.py", "allow"),
            ("cat src/other.py", "block"),
            ("rg VALUE src/constants.py", "block"),
            ("cat src/constants.py; cat src/other.py", "block"),
            ("rg VALUE dist/assets", "allow"),
            ("rg VALUE dist/assets; rg VALUE src", "block"),
        ):
            response = await engine.evaluate(
                self._normalized_bash_event(command, cwd=str(tmp_path), project_path=str(tmp_path)),
                session_id=SESSION_ID,
                variables=variables,
            )
            assert response.decision == decision, command

    def test_gcode_fail_open_tracker_rules_sync(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)

        tracker = manager.get_by_name("track-gcode-fail-open")
        assert tracker is not None
        tracker_body = RuleDefinitionBody.model_validate(tracker.definition_json)
        assert tracker_body.event.value == "after_tool"
        assert tracker_body.when is not None
        assert "event.data.get('canonical_code_index_recovery')" in tracker_body.when
        assert tracker_body.effects is not None
        assert tracker_body.effects[0].type == "set_variable"
        assert tracker_body.effects[0].variable == "code_index_recoveries"
        assert "canonical_code_index_recovery" in str(tracker_body.effects[0].value)

        attempts = manager.get_by_name("track-code-index-attempts")
        assert attempts is not None
        attempts_body = RuleDefinitionBody.model_validate(attempts.definition_json)
        assert attempts_body.event.value == "before_tool"
        assert attempts_body.effects is not None
        assert attempts_body.effects[0].variable == "code_index_attempts"
        # A blocked gcode call must not count as an attempt: later rules keep only blocks.
        block_priorities = [
            row.priority
            for row in manager.list_all()
            if (body := RuleDefinitionBody.model_validate(row.definition_json)).event.value
            == "before_tool"
            and any(effect.type == "block" for effect in body.resolved_effects)
        ]
        assert max(block_priorities) < attempts.priority

        reset = manager.get_by_name("reset-code-index-navigation")
        assert reset is not None
        reset_body = RuleDefinitionBody.model_validate(reset.definition_json)
        assert reset_body.effects is not None
        cleared = {effect.variable: effect.value for effect in reset_body.effects}
        for variable in ("code_index_recoveries", "code_index_attempts", "code_index_verified"):
            assert cleared[variable] == []

    @pytest.mark.asyncio
    async def test_compound_search_after_pipeline_uses_persistent_shell_cwd(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        scratchpad = tmp_path / "scratchpad"
        command_template = (
            "cd {root} && "
            "echo alpha > first.txt && "
            "grep -r alpha . && "
            "echo beta > second.txt && "
            "grep -r beta . | head -1 && "
            "echo gamma > third.txt && "
            "grep -r gamma ."
        )
        expected_names = ("first.txt", "second.txt", "third.txt")

        for loaded in (False, True):
            event = self._normalized_bash_event(
                command_template.format(root=scratchpad),
                cwd=str(repo),
                project_path=str(repo),
            )
            assert event.data["canonical_file_paths"] == [
                str(scratchpad / name) for name in expected_names
            ]
            assert event.data["canonical_code_navigation_repo_scope"] is False

            response = await RuleEngine(db).evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )

            assert response.decision == "allow"

        for loaded in (False, True):
            event = self._normalized_bash_event(
                command_template.format(root=repo),
                cwd=str(repo),
                project_path=str(repo),
            )
            assert event.data["canonical_file_paths"] == [
                str(repo / name) for name in expected_names
            ]
            assert event.data["canonical_code_navigation_repo_scope"] is True

            response = await RuleEngine(db).evaluate(
                event,
                session_id=SESSION_ID,
                variables=self._variables(loaded=loaded),
            )

            assert response.decision == ("allow" if loaded else "block")

    @pytest.mark.asyncio
    async def test_normalized_repo_search_still_blocks(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._normalized_bash_event(
            "rg pattern src",
            cwd=str(repo),
            project_path=str(repo),
        )

        assert event.data["canonical_code_navigation_repo_scope"] is True
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_all_gcode_with_echo_separator_is_allowed(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._normalized_bash_event(
            'gcode grep "a" src -m 10 ; echo === ; gcode grep "b" src -m 10',
            cwd=str(repo),
            project_path=str(repo),
        )

        assert event.data["canonical_code_index_navigation"] is True
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_gcode_with_stderr_suppression_is_allowed(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._normalized_bash_event(
            'gcode grep "pattern" src -m 50 2>/dev/null',
            cwd=str(repo),
            project_path=str(repo),
        )

        assert event.data["canonical_code_index_navigation"] is True
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_gcode_search_with_python_formatter_is_allowed(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._normalized_bash_event(
            'gcode search "search_by_stored_vectors" --limit 10 2>/dev/null '
            '| python3 -c "import json,sys; d=json.load(sys.stdin); '
            "[print(r['file_path'], r['line_start']) for r in d['results']]\"",
            cwd=str(repo),
            project_path=str(repo),
        )

        # The read-only interpreter payload is a pipeline filter, so the whole
        # pipeline still normalizes to gcode navigation. #19250's guarantee is
        # that it is allowed, which the rule-level shell_command_invokes_gcode
        # guard provides; the flag itself simply describes the command.
        assert event.data.get("canonical_code_index_navigation") is True
        assert event.data.get("canonical_code_index_command") == "gcode search"
        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_shell_search_with_stderr_suppression_still_blocks(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._normalized_bash_event(
            "rg pattern src 2>/dev/null",
            cwd=str(repo),
            project_path=str(repo),
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_unnormalized_repo_search_with_cwd_and_project_path_still_blocks(
        self,
        db: HubDatabase,
        tmp_path: Path,
    ) -> None:
        _sync_bundled(db)
        repo = tmp_path / "repo"
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "rg pattern src", "cwd": str(repo)},
                "cwd": str(repo),
                "project_path": str(repo),
                "canonical_tool_kind": "search",
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_gcode_navigation_is_allowed_and_sets_turn_flag(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        command = 'gcode grep "pattern" src -m 50'
        before = self._normalized_bash_event(command, cwd=str(tmp_path), project_path=str(tmp_path))
        after = self._normalized_bash_event(command, cwd=str(tmp_path), project_path=str(tmp_path))
        after.data["tool_output"] = "src/app.py:1:pattern"
        normalize_tool_fields(after.data)
        after.event_type = HookEventType.AFTER_TOOL

        allowed = await RuleEngine(db).evaluate(before, session_id=SESSION_ID, variables=variables)
        await RuleEngine(db).evaluate(after, session_id=SESSION_ID, variables=variables)

        assert allowed.decision == "allow"
        assert variables["code_index_navigation_used_this_turn"] is True
        assert variables["code_index_attempts"]
        assert variables["code_index_verified"] == variables["code_index_attempts"]

    @pytest.mark.asyncio
    async def test_source_read_redirect_fails_open_after_unverified_gcode(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._variables(loaded=True)
        root = str(tmp_path)

        async def decide(command: str, output: str | None = None) -> str:
            event = self._normalized_bash_event(command, cwd=root, project_path=root)
            if output is not None:
                event.data["tool_output"] = output
                normalize_tool_fields(event.data)
                event.event_type = HookEventType.AFTER_TOOL
            response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
            return response.decision

        redirected = await engine.evaluate(
            self._normalized_bash_event("cat src/app.py", cwd=root, project_path=root),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert redirected.decision == "block"
        assert "[prefer-gcode-for-source-read]" in (redirected.reason or "")
        for command in ("sed -n '1,40p' src/app.py", "rg pattern src", "gcode outline src/app.py"):
            assert await decide(command) == "allow", command
        assert await decide("cat src/app.py") == "allow"
        assert await decide("cat src/other.py") == "block"

        await decide("gcode outline src/app.py", "src/app.py:1-3 [function] main")
        assert await decide("cat src/app.py") == "block"

        await decide(
            "gcode outline src/other.py",
            '{"error":"schema_mismatch","message":"schema identity mismatch"}',
        )
        assert await decide("cat src/other.py") == "allow"

    @pytest.mark.parametrize(
        ("read_range", "decision"),
        [({}, "block"), ({"offset": 10, "limit": 40}, "allow")],
    )
    @pytest.mark.asyncio
    async def test_native_read_redirect_spares_short_ranges(
        self, db: HubDatabase, tmp_path: Path, read_range: dict[str, int], decision: str
    ) -> None:
        _sync_bundled(db)
        data: dict[str, Any] = {
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / "src/app.py"), **read_range},
            "cwd": str(tmp_path),
            "project_path": str(tmp_path),
        }
        normalize_tool_fields(data)

        response = await RuleEngine(db).evaluate(
            self._event(HookEventType.BEFORE_TOOL, data),
            session_id=SESSION_ID,
            variables=self._variables(loaded=True),
        )

        assert response.decision == decision

    @pytest.mark.asyncio
    async def test_direct_gcode_call_does_not_require_skill_loading(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = self._normalized_bash_event('gcode search "concept" src')

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_code_index_navigation"] is True
        assert response.decision == "allow"

    @pytest.mark.parametrize(
        ("rule_name", "command"),
        [
            ("require-code-index-skill", 'gcode search-content "query" src'),
            ("require-code-index-skill", 'gcode grep "pattern" src -m 50'),
            ("require-code-index-skill", "gcode tree src"),
            ("require-code-index-skill", "gcode outline src/gobby/workflows/engine/core.py"),
            ("prefer-gcode-for-source-read", "gcode outline src/gobby/workflows/engine/core.py"),
            (
                "prefer-gcode-for-source-read",
                "gcode symbol-at src/gobby/workflows/engine/core.py:10",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_gcode_navigation_remediation_clears_same_tool_retry_guard(
        self, db: HubDatabase, rule_name: str, command: str
    ) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        variables.update(
            {
                "_last_blocked_tool": "Bash",
                "_last_blocked_rule_name": rule_name,
                "_last_blocked_reason": f"Rule enforced by Gobby: [{rule_name}]\nUse gcode",
                "consecutive_tool_blocks": 3,
                "max_consecutive_blocked_tool_attempts": 5,
            }
        )
        event = self._normalized_bash_event(command)

        assert event.data["canonical_code_index_navigation"] is True

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "allow"
        assert "consecutive-tool-block" not in (response.reason or "")
        assert variables["consecutive_tool_blocks"] == 0
        assert variables["_last_blocked_tool"] == ""
        assert variables["_last_blocked_rule_name"] == ""
        assert variables["_last_blocked_reason"] == ""

    @pytest.mark.asyncio
    async def test_turn_start_resets_gcode_navigation_flag(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False, used=True)
        variables["turn_written_paths"] = ["src/app.py"]
        event = self._event(HookEventType.BEFORE_AGENT, {"prompt": "continue"})

        await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)

        assert variables["code_index_navigation_used_this_turn"] is False
        assert variables["turn_written_paths"] == []

    @pytest.mark.asyncio
    async def test_first_broad_and_tight_reads_teach_code_index(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._variables(loaded=False)
        broad = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "cat src/app.py",
                "canonical_tool_kind": "read",
                "canonical_file_path": "src/app.py",
                "canonical_code_navigation_action": "read",
                "canonical_code_navigation_broad": True,
                "canonical_source_read_scope": "full_file",
            },
        )
        narrow = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "sed -n '1,40p' src/app.py",
                "canonical_tool_kind": "read",
                "canonical_file_path": "src/app.py",
                "canonical_code_navigation_action": "read",
                "canonical_code_navigation_broad": False,
                "canonical_narrow_source_context": True,
                "canonical_source_line_count": 40,
                "canonical_source_read_scope": "line_range",
            },
        )

        broad_response = await engine.evaluate(broad, session_id=SESSION_ID, variables=variables)
        narrow_response = await engine.evaluate(narrow, session_id=SESSION_ID, variables=variables)

        assert broad_response.decision == "block"
        assert skill_fetch_directive("gobby:references/code-index/overview.md") in (
            broad_response.reason or ""
        )
        assert narrow_response.decision == "block"

    @pytest.mark.asyncio
    async def test_first_plain_sed_range_teaches_code_index(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = self._normalized_bash_event("sed -n '10,40p' src/gobby/hooks/events.py")

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_source_line_count"] == 31
        assert event.data["canonical_narrow_source_context"] is True
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_wc_line_count_is_explicitly_allowed(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = self._normalized_bash_event("wc -l src/gobby/hooks/events.py")

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert event.data["canonical_tool_kind"] == "execute"
        assert "canonical_code_navigation_action" not in event.data
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_compound_broad_shell_read_and_search_block(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._variables(loaded=False)

        search_response = await engine.evaluate(
            self._normalized_bash_event("cd dir && rg pattern src"),
            session_id=SESSION_ID,
            variables=variables,
        )
        read_response = await engine.evaluate(
            self._normalized_bash_event("cd dir; cat app.py"),
            session_id=SESSION_ID,
            variables=variables,
        )

        assert search_response.decision == "block"
        assert read_response.decision == "block"

    @pytest.mark.asyncio
    async def test_first_compound_narrow_read_teaches_code_index(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = self._normalized_bash_event("cd dir\nsed -n '1,40p' app.py")

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False),
        )

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_wide_line_read_requires_prior_gcode_navigation(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "sed -n '1,80p' src/app.py",
                "canonical_tool_kind": "read",
                "canonical_file_path": "src/app.py",
                "canonical_code_navigation_action": "read",
                "canonical_code_navigation_broad": True,
                "canonical_source_line_count": 80,
                "canonical_source_read_scope": "line_range",
            },
        )

        blocked = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False, used=False),
        )
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=self._variables(loaded=False, used=True),
        )

        assert blocked.decision == "block"
        assert allowed.decision == "allow"


@pytest.mark.asyncio
async def test_reference_contract_4_1_1(db: HubDatabase) -> None:
    """Bootstrap preserves order, handoff timing, and the exact memory failure exception."""
    _sync_bundled(db)
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
            ("bootstrap-default-agent-core-skills",),
        )
    engine = RuleEngine(db)
    requirements = (
        "gobby:references/skills/loading.md",
        "gobby:references/memory/overview.md",
        "brevity",
        "restraint",
    )
    event = TestDefaultAgentCoreSkillBootstrap._turn_event()
    for pending, failed, custom in (
        (True, False, False),
        (False, False, False),
        (False, True, False),
        (False, True, True),
    ):
        errors = [_skill_tool_error_record(requirements[1])] if failed else []
        if custom:
            errors[0]["target_key"] = extract_target_key(
                {"tool_name": "get_skill_file"},
                {"name": "custom", "path": "references/memory/overview.md"},
            )
        response = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "loaded_skills": ["gobby"],
                "handoff_pull_pending": pending,
                "open_tool_errors": errors,
            },
        )
        context = response.context or ""
        expected = (
            []
            if pending
            else [
                requirement
                for requirement in requirements
                if not (failed and not custom and requirement == requirements[1])
            ]
        )
        for requirement in requirements:
            assert (skill_fetch_directive(requirement) in context) is (requirement in expected)
        positions = [context.index(skill_fetch_directive(item)) for item in expected]
        assert positions == sorted(positions)
    response = await engine.evaluate(
        event,
        session_id=SESSION_ID,
        variables={
            "loaded_skills": list(requirements[2:]),
            "loaded_skill_references": list(requirements[:2]),
        },
    )
    assert not response.context
