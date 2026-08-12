"""Tests for worker-safety rules in new RuleDefinitionBody format.

Verifies the migrated worker-safety.yaml produces identical blocking
behavior to the old rule_definitions format.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> dict[str, object]:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


class TestWorkerSafetySync:
    """Test that the bundled worker-safety.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All worker-safety rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        expected = {"no-push", "no-force-push", "no-destructive-git"}
        assert expected.issubset(rule_names), f"Missing: {expected - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All worker-safety rules should have group='worker-safety'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            body = json.loads(row.definition_json)
            if row.name in {"no-push", "no-force-push", "no-destructive-git"}:
                assert body.get("group") == "worker-safety", f"{row.name} missing group"

    def test_agent_scope_persists_through_sync(self, db, manager) -> None:
        """agent_scope from YAML should be preserved in definition_json."""
        _sync_bundled(db)

        row = manager.get_by_name("no-push-for-workers")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.agent_scope == ["developer", "qa-reviewer", "doc-reviewer"]

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in {"no-push", "no-force-push", "no-destructive-git"}:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                assert body.event.value == "before_tool"
                assert body.effects[0].type == "block"


class TestNoPushRule:
    """Verify no-push rule blocks git push commands."""

    def test_blocks_bash_with_git_push(self, db, manager) -> None:
        """no-push should block Bash tool with git push."""
        _sync_bundled(db)

        row = manager.get_by_name("no-push")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effects[0].tools == ["Bash"]
        assert body.effects[0].command_pattern is not None
        assert "push" in body.effects[0].command_pattern


class TestNoForcePushRule:
    """Verify no-force-push rule blocks force push commands."""

    def test_blocks_force_push_flags(self, db, manager) -> None:
        """no-force-push should block force push flags."""
        _sync_bundled(db)

        row = manager.get_by_name("no-force-push")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effects[0].tools == ["Bash"]
        assert body.effects[0].command_pattern is not None
        assert "--force" in body.effects[0].command_pattern


class TestNoDestructiveGitRule:
    """Verify no-destructive-git rule blocks dangerous git commands."""

    def test_blocks_destructive_commands(self, db, manager) -> None:
        """no-destructive-git should block reset --hard, clean -f, etc."""
        _sync_bundled(db)

        row = manager.get_by_name("no-destructive-git")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effects[0].tools == ["Bash"]
        assert body.effects[0].command_pattern is not None
        assert "reset" in body.effects[0].command_pattern


class TestDockerPolicyApprovalRule:
    """Verify Docker policy writes require direct, one-turn user approval."""

    @staticmethod
    def _isolated_engine(db: HubDatabase) -> RuleEngine:
        _sync_bundled(db)
        db.execute(
            "DELETE FROM workflow_definitions "
            "WHERE name NOT IN "
            "('set-docker-policy-approval-for-turn', 'require-docker-policy-approval')"
        )
        return RuleEngine(db)

    @staticmethod
    def _event(event_type: HookEventType, data: dict[str, object]) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data=data,
        )

    @pytest.mark.asyncio
    async def test_approval_requires_exact_interactive_user_prompt(self, db: HubDatabase) -> None:
        engine = self._isolated_engine(db)
        variables: dict[str, object] = {}

        await engine.evaluate(
            self._event(HookEventType.BEFORE_AGENT, {"prompt": "/approve-docker-policy"}),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert variables["docker_policy_user_approved_this_turn"] is True

        await engine.evaluate(
            self._event(
                HookEventType.BEFORE_AGENT,
                {"prompt": "please use /approve-docker-policy"},
            ),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert variables["docker_policy_user_approved_this_turn"] is False

        variables["is_spawned_agent"] = True
        await engine.evaluate(
            self._event(HookEventType.BEFORE_AGENT, {"prompt": "/approve-docker-policy"}),
            session_id=SESSION_ID,
            variables=variables,
        )
        assert variables["docker_policy_user_approved_this_turn"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "Dockerfile",
            "containers/Dockerfile.dev",
            "src/gobby/data/docker-compose.services.yml",
            "deploy/compose.prod.yaml",
            ".dockerignore",
            "docker-bake.hcl",
            ".docker/config.json",
        ],
    )
    async def test_protected_paths_require_approval(self, db: HubDatabase, path: str) -> None:
        engine = self._isolated_engine(db)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "canonical_tool_kind": "write",
                "canonical_file_path": path,
                "canonical_file_paths": [path],
                "tool_input": {"file_path": path},
                "tool_name": "Edit",
            },
        )

        blocked = await engine.evaluate(event, session_id=SESSION_ID, variables={})
        allowed = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={"docker_policy_user_approved_this_turn": True},
        )

        assert blocked.decision == "block"
        assert blocked.reason is not None
        assert "/approve-docker-policy" in blocked.reason
        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_multi_path_patch_and_unrelated_write(self, db: HubDatabase) -> None:
        engine = self._isolated_engine(db)
        protected = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_input": (
                    "*** Begin Patch\n"
                    "*** Update File: README.md\n"
                    "@@\n"
                    "*** Update File: ops/docker-compose.yml\n"
                    "@@\n"
                    "*** End Patch\n"
                ),
                "tool_name": "apply_patch",
            },
        )
        unrelated = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "canonical_tool_kind": "write",
                "canonical_file_paths": ["README.md"],
                "tool_input": {"file_path": "README.md"},
                "tool_name": "Edit",
            },
        )

        assert (
            await engine.evaluate(protected, session_id=SESSION_ID, variables={})
        ).decision == "block"
        assert (
            await engine.evaluate(unrelated, session_id=SESSION_ID, variables={})
        ).decision == "allow"


class TestNoFullVitestSuiteRule:
    """Verify full-suite Vitest runs are blocked without blocking focused files."""

    def _effect(
        self,
        db: HubDatabase,
        manager: LocalWorkflowDefinitionManager,
    ) -> RuleEffect:
        _sync_bundled(db)

        row = manager.get_by_name("no-full-vitest-suite")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        return body.effects[0]

    @staticmethod
    def _is_blocked(effect: RuleEffect, command: str) -> bool:
        assert effect.command_pattern is not None
        if not re.search(effect.command_pattern, command):
            return False
        if effect.command_not_pattern and re.search(effect.command_not_pattern, command):
            return False
        return True

    @pytest.mark.parametrize(
        "command",
        [
            "npx vitest run",
            "cd web && npx vitest run --no-coverage",
            "jest --runInBand",
        ],
    )
    def test_blocks_unscoped_vitest_and_jest_runs(self, db, manager, command: str) -> None:
        effect = self._effect(db, manager)

        assert self._is_blocked(effect, command)

    @pytest.mark.parametrize(
        "command",
        [
            (
                "cd web && npx vitest run "
                "src/components/dashboard/__tests__/SystemHealthCard.test.tsx"
            ),
            (
                "cd web && npx vitest run --no-coverage "
                "src/components/memory/__tests__/MemoryPage.falkordb.test.tsx "
                "src/hooks/__tests__/useMemory.test.ts"
            ),
            "npx vitest run src/hooks/useMemory.spec.ts",
            "jest --runInBand --testPathPattern=SystemHealthCard",
            "jest -t 'renders FalkorDB status'",
        ],
    )
    def test_allows_focused_vitest_and_jest_runs(self, db, manager, command: str) -> None:
        effect = self._effect(db, manager)

        assert not self._is_blocked(effect, command)


class TestNoFullCargoSuiteRule:
    """Verify full-suite Cargo test runs are blocked without blocking focused runs."""

    def _effect(
        self,
        db: HubDatabase,
        manager: LocalWorkflowDefinitionManager,
    ) -> RuleEffect:
        _sync_bundled(db)

        row = manager.get_by_name("no-full-cargo-test")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        return body.effects[0]

    @staticmethod
    def _is_blocked(effect: RuleEffect, command: str) -> bool:
        assert effect.command_pattern is not None
        if not re.search(effect.command_pattern, command):
            return False
        if effect.command_not_pattern and re.search(effect.command_not_pattern, command):
            return False
        return True

    @pytest.mark.parametrize(
        "command",
        [
            "cargo test",
            "cargo test --no-default-features",
            "cargo test --workspace",
            "cargo test --all-features",
        ],
    )
    def test_blocks_unscoped_cargo_test_runs(
        self,
        db: HubDatabase,
        manager: LocalWorkflowDefinitionManager,
        command: str,
    ) -> None:
        effect = self._effect(db, manager)

        assert self._is_blocked(effect, command)
        assert effect.reason is not None
        assert "cargo test -p <package>" in effect.reason

    @pytest.mark.parametrize(
        "command",
        [
            "cargo test -p gobby-code --no-default-features",
            "cargo test --package gobby-core search::tests",
            "cargo test graph_report -p gcode",
            "cargo test --test cli_graph_report",
            "cargo test --bin gcode graph_report",
        ],
    )
    def test_allows_focused_cargo_test_runs(
        self,
        db: HubDatabase,
        manager: LocalWorkflowDefinitionManager,
        command: str,
    ) -> None:
        effect = self._effect(db, manager)

        assert not self._is_blocked(effect, command)
