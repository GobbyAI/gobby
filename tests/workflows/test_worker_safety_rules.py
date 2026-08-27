"""Tests for worker-safety rules in new RuleDefinitionBody format.

Verifies the migrated worker-safety.yaml produces identical blocking
behavior to the old rule_definitions format.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_ROOT = REPO_ROOT / "src/gobby/install/shared/workflows/rules"
RULES_REFERENCE = RULES_ROOT / "AGENTS.md"
MANAGED_GIT_RULES = {
    "block-git-clone",
    "block-git-clone-interactive",
    "block-git-worktree-mutations",
    "block-git-worktree-mutations-interactive",
}
EXPECTED_WORKER_SAFETY_RULES = {
    "no-push",
    "no-force-push",
    "no-destructive-git",
} | MANAGED_GIT_RULES
GIT_GLOBAL_OPTION_PREFIXES = (
    "git -C /repo",
    "git -c core.pager=cat",
    "git --git-dir=/repo/.git",
    "git --git-dir /repo/.git",
    "git --work-tree=/repo",
    "git --work-tree /repo",
    "git --namespace=task",
    "git --namespace task",
    "git --exec-path=/usr/libexec/git-core",
    "git --exec-path /usr/libexec/git-core",
    "git --no-pager",
    "git --paginate",
    "git -p",
    "git --bare",
    "git --literal-pathspecs",
    "git --no-optional-locks",
    "git --no-pager -C /repo",
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> dict[str, object]:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


def _get_rule(
    manager: RuleDefinitionManager,
    name: str,
) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None, f"Rule {name!r} not found after sync"
    return RuleDefinitionBody.model_validate(row.definition_json)


def _rule_matches(body: RuleDefinitionBody, command: str) -> bool:
    return any(
        effect.command_pattern is not None and re.search(effect.command_pattern, command)
        for effect in body.resolved_effects
    )


def _first_effect(body: RuleDefinitionBody) -> RuleEffect:
    effects = body.resolved_effects
    assert effects
    return effects[0]


def _yaml_rule_count(group: str) -> int:
    count = 0
    for path in sorted((RULES_ROOT / group).glob("*.yaml")):
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path} must contain a YAML mapping"
        rules = data.get("rules")
        assert isinstance(rules, dict), f"{path} must contain a rules mapping"
        count += len(rules)
    return count


@pytest.mark.parametrize(
    ("group", "expected_count"),
    (("task-enforcement", 20), ("worker-safety", 55)),
)
def test_rule_reference_counts_match_yaml(group: str, expected_count: int) -> None:
    prefix = f"| `{group}` |"
    matching_lines = [
        line
        for line in RULES_REFERENCE.read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]
    assert len(matching_lines) == 1
    columns = [column.strip() for column in matching_lines[0].strip("|").split("|")]

    assert _yaml_rule_count(group) == int(columns[2]) == expected_count


class TestWorkerSafetySync:
    """Test that the bundled worker-safety.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All worker-safety rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        expected = EXPECTED_WORKER_SAFETY_RULES
        assert expected.issubset(rule_names), f"Missing: {expected - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All worker-safety rules should have group='worker-safety'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            body = row.definition_json
            if row.name in EXPECTED_WORKER_SAFETY_RULES:
                assert body.get("group") == "worker-safety", f"{row.name} missing group"

    def test_agent_scope_persists_through_sync(self, db, manager) -> None:
        """agent_scope from YAML should be preserved in definition_json."""
        _sync_bundled(db)

        row = manager.get_by_name("no-push-for-workers")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.agent_scope == ["developer", "qa-reviewer", "doc-reviewer"]

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in EXPECTED_WORKER_SAFETY_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                assert body.event.value == "before_tool"
                assert body.effects[0].type == "block"


class TestNoPushRule:
    """Verify no-push rule blocks git push commands."""

    def test_blocks_bash_with_git_push(self, db, manager) -> None:
        """no-push should block Bash tool with git push."""
        _sync_bundled(db)

        row = manager.get_by_name("no-push")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
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

        body = RuleDefinitionBody.model_validate(row.definition_json)
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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.effects[0].tools == ["Bash"]
        assert body.effects[0].command_pattern is not None
        assert "reset" in body.effects[0].command_pattern


class TestManagedGitIsolationRules:
    rules: dict[str, RuleDefinitionBody]

    @pytest.fixture(autouse=True)
    def _load_rules(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        self.rules = {name: _get_rule(manager, name) for name in MANAGED_GIT_RULES}

    @pytest.mark.parametrize(
        "subcommand",
        ("add ../task", "remove ../task", "move ../old ../new", "prune", "repair"),
    )
    def test_blocks_worktree_mutations(self, subcommand: str) -> None:
        command = f"git worktree {subcommand}"
        assert _rule_matches(self.rules["block-git-worktree-mutations"], command)
        assert _rule_matches(self.rules["block-git-worktree-mutations-interactive"], command)

    @pytest.mark.parametrize(
        "command",
        (
            "git clone https://example.com/repo.git",
            "git clone --depth=1 https://example.com/repo.git ./repo",
            "/usr/bin/git clone https://example.com/repo.git",
        ),
    )
    def test_blocks_clone(self, command: str) -> None:
        assert _rule_matches(self.rules["block-git-clone"], command)
        assert _rule_matches(self.rules["block-git-clone-interactive"], command)

    @pytest.mark.parametrize("prefix", GIT_GLOBAL_OPTION_PREFIXES)
    def test_blocks_supported_git_global_options(self, prefix: str) -> None:
        assert _rule_matches(
            self.rules["block-git-worktree-mutations"],
            f"{prefix} worktree prune",
        )
        assert _rule_matches(
            self.rules["block-git-clone"],
            f"{prefix} clone https://example.com/repo.git",
        )

    @pytest.mark.parametrize(
        "command",
        (
            "git worktree list",
            "git worktree lock ../task",
            "git worktree unlock ../task",
            "git -C /repo worktree list --porcelain",
            "git worktree --help",
            "git worktree addendum",
        ),
    )
    def test_allows_nonmutating_worktree_commands(self, command: str) -> None:
        assert not _rule_matches(self.rules["block-git-worktree-mutations"], command)
        assert not _rule_matches(self.rules["block-git-worktree-mutations-interactive"], command)

    @pytest.mark.parametrize(
        "command",
        (
            "git status",
            "git submodule add https://example.com/repo.git vendor/repo",
            "git cloneable https://example.com/repo.git",
            "git-clone https://example.com/repo.git",
        ),
    )
    def test_allows_commands_that_are_not_git_clone(self, command: str) -> None:
        assert not _rule_matches(self.rules["block-git-clone"], command)
        assert not _rule_matches(self.rules["block-git-clone-interactive"], command)

    @pytest.mark.parametrize(
        ("worker_name", "interactive_name"),
        (
            ("block-git-clone", "block-git-clone-interactive"),
            (
                "block-git-worktree-mutations",
                "block-git-worktree-mutations-interactive",
            ),
        ),
    )
    def test_worker_and_interactive_rule_shape(
        self,
        manager: RuleDefinitionManager,
        worker_name: str,
        interactive_name: str,
    ) -> None:
        worker = self.rules[worker_name]
        interactive = self.rules[interactive_name]
        worker_row = manager.get_by_name(worker_name)
        interactive_row = manager.get_by_name(interactive_name)
        assert worker_row is not None and interactive_row is not None

        assert worker.when == "variables.get('is_spawned_agent')"
        assert interactive.when == "not variables.get('is_spawned_agent')"
        worker_effect = _first_effect(worker)
        interactive_effect = _first_effect(interactive)
        assert worker_effect.command_pattern == interactive_effect.command_pattern
        assert worker_row.priority == 50
        assert interactive_row.priority == 55
        assert "worker-safety" in (worker_row.tags or [])
        assert "default" not in (worker_row.tags or [])
        assert "default" in (interactive_row.tags or [])
        assert "Ask the user for permission to disable this rule" not in (
            worker_effect.reason or ""
        )
        assert "Ask the user for permission to disable this rule" in (
            interactive_effect.reason or ""
        )

    def test_reasons_route_to_managed_workspace_tools(self) -> None:
        worktree_reason = _first_effect(self.rules["block-git-worktree-mutations"]).reason or ""
        clone_reason = _first_effect(self.rules["block-git-clone"]).reason or ""

        assert "gobby-worktrees.create_worktree" in worktree_reason
        assert "gobby-worktrees.delete_worktree" in worktree_reason
        assert "worktree_path" in worktree_reason
        assert "gobby-clones.create_clone" in clone_reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "spawned"),
        (
            ("git clone https://example.com/repo.git", True),
            ("git clone https://example.com/repo.git", False),
            ("git worktree add ../task", True),
            ("git worktree add ../task", False),
        ),
    )
    async def test_audience_condition_selects_matching_rule(
        self,
        db: HubDatabase,
        command: str,
        spawned: bool,
    ) -> None:
        db.execute(
            "DELETE FROM rule_definitions WHERE name NOT IN "
            "('block-git-clone', 'block-git-clone-interactive', "
            "'block-git-worktree-mutations', "
            "'block-git-worktree-mutations-interactive')"
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "command": command,
                "tool_input": {"command": command},
                "tool_name": "Bash",
            },
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"is_spawned_agent": spawned},
        )

        assert response.decision == "block"
        reason = response.reason or ""
        assert ("Ask the user for permission to disable this rule" in reason) is not spawned


class TestDockerPolicyBlockRule:
    """Verify Docker policy writes are blocked until the installed rule is toggled off."""

    @staticmethod
    def _isolated_engine(db: HubDatabase) -> RuleEngine:
        _sync_bundled(db)
        db.execute("DELETE FROM rule_definitions WHERE name != 'block-docker-policy-edits'")
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

    def test_rule_is_one_enabled_plain_block(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        self._isolated_engine(db)

        row = manager.get_by_name("block-docker-policy-edits")
        assert row is not None
        assert row.enabled is True
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "canonical_tool_kind" in body.when
        assert "touches_docker_policy_path" in body.when
        assert "canonical_file_paths" in body.when
        assert "variables" not in body.when
        assert "ordinary MCP tool" in (row.description or "")
        assert "unknown-scope content mutations" in (row.description or "").lower()
        effects = body.effects
        assert effects is not None
        assert len(effects) == 1
        assert effects[0].type == "block"

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
    async def test_protected_paths_are_blocked(self, db: HubDatabase, path: str) -> None:
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

        assert blocked.decision == "block"
        assert blocked.reason is not None
        assert "toggle_rule('block-docker-policy-edits', enabled=false)" in blocked.reason
        assert "gobby rules toggle" in blocked.reason

    @pytest.mark.asyncio
    async def test_toggle_rule_off_allows_write_and_on_blocks_again(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        from gobby.mcp_proxy.tools.workflows._rules import toggle_rule

        engine = self._isolated_engine(db)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "canonical_tool_kind": "write",
                "canonical_file_paths": ["Dockerfile"],
                "tool_input": {"file_path": "Dockerfile"},
                "tool_name": "Edit",
            },
        )

        assert (
            await engine.evaluate(event, session_id=SESSION_ID, variables={})
        ).decision == "block"
        assert toggle_rule(manager, name="block-docker-policy-edits", enabled=False)["success"]
        assert (
            await engine.evaluate(event, session_id=SESSION_ID, variables={})
        ).decision == "allow"
        assert toggle_rule(manager, name="block-docker-policy-edits", enabled=True)["success"]
        assert (
            await engine.evaluate(event, session_id=SESSION_ID, variables={})
        ).decision == "block"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "target"),
        [
            ("echo x > Dockerfile", "Dockerfile"),
            ("printf x | tee ops/compose-prod.yml", "ops/compose-prod.yml"),
            ("cp template.yml deploy/podman-compose.yml", "deploy/podman-compose.yml"),
            (
                "python -c \"from pathlib import Path; Path('Dockerfile').write_text('x')\"",
                "Dockerfile",
            ),
            (
                "python3 - <<'PYEOF'\nfrom pathlib import Path\n"
                "Path('docker-compose.yml').write_text('x')\nPYEOF",
                "docker-compose.yml",
            ),
        ],
    )
    async def test_canonical_shell_writes_are_blocked(
        self,
        db: HubDatabase,
        command: str,
        target: str,
    ) -> None:
        engine = self._isolated_engine(db)
        data: dict[str, object] = {
            "tool_input": {"command": command},
            "tool_name": "Bash",
        }
        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        canonical_file_paths = data["canonical_file_paths"]
        assert isinstance(canonical_file_paths, list)
        assert target in canonical_file_paths
        response = await engine.evaluate(
            self._event(HookEventType.BEFORE_TOOL, data),
            session_id=SESSION_ID,
            variables={},
        )
        assert response.decision == "block"
        assert response.reason is not None
        assert "Docker and Compose policy edits are operator-controlled" in response.reason
        assert "unknown scope" not in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "python3 - <<'PYEOF'\nfrom pathlib import Path\n"
            "Path('.gobby/plans/a.md').write_text('x')\nPath('docs/b.md').write_text('y')\nPYEOF",
            "python -c \"open('notes.md', 'w').write('x')\"",
        ],
    )
    async def test_python_writes_to_literal_non_docker_paths_are_allowed(
        self,
        db: HubDatabase,
        command: str,
    ) -> None:
        engine = self._isolated_engine(db)
        data: dict[str, object] = {
            "tool_input": {"command": command},
            "tool_name": "Bash",
        }
        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_paths"]
        response = await engine.evaluate(
            self._event(HookEventType.BEFORE_TOOL, data),
            session_id=SESSION_ID,
            variables={},
        )
        assert response.decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "git apply update.patch",
            "patch -p1 < update.patch",
            "python3 - <<'PYEOF'\nimport sys\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text('x')\nPYEOF",
        ],
    )
    async def test_unknown_scope_content_mutations_are_blocked(
        self,
        db: HubDatabase,
        command: str,
    ) -> None:
        engine = self._isolated_engine(db)
        data: dict[str, object] = {
            "tool_input": {"command": command},
            "tool_name": "Bash",
        }
        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert not data.get("canonical_file_paths")
        response = await engine.evaluate(
            self._event(HookEventType.BEFORE_TOOL, data),
            session_id=SESSION_ID,
            variables={},
        )
        assert response.decision == "block"
        assert response.reason is not None
        assert "content mutation of unknown scope" in response.reason
        assert "Docker and Compose policy edits" not in response.reason
        assert "toggle_rule('block-docker-policy-edits', enabled=false)" in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "target"),
        [
            ("git checkout -- Dockerfile", "Dockerfile"),
            ("git restore -- deploy/compose.yaml", "deploy/compose.yaml"),
            ("git revert HEAD", None),
        ],
    )
    async def test_git_content_mutations_are_blocked(
        self,
        db: HubDatabase,
        command: str,
        target: str | None,
    ) -> None:
        engine = self._isolated_engine(db)
        data: dict[str, object] = {
            "tool_input": {"command": command},
            "tool_name": "Bash",
        }
        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        if target is not None:
            canonical_file_paths = data.get("canonical_file_paths")
            assert isinstance(canonical_file_paths, list)
            assert target in canonical_file_paths
        response = await engine.evaluate(
            self._event(HookEventType.BEFORE_TOOL, data),
            session_id=SESSION_ID,
            variables={},
        )
        assert response.decision == "block"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "git add -- Dockerfile",
            "git commit -m 'authorized Docker edit' --only -- Dockerfile",
            (
                "git add -- Dockerfile && "
                "git commit -m 'authorized Docker edit' --only -- Dockerfile"
            ),
        ],
    )
    async def test_staging_and_committing_authorized_paths_are_allowed(
        self,
        db: HubDatabase,
        command: str,
    ) -> None:
        engine = self._isolated_engine(db)
        data: dict[str, object] = {
            "tool_input": {"command": command},
            "tool_name": "Bash",
        }
        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        response = await engine.evaluate(
            self._event(HookEventType.BEFORE_TOOL, data),
            session_id=SESSION_ID,
            variables={},
        )
        assert response.decision == "allow"

        if "git add" in command:
            assert data["canonical_repo_mutation"] is True
            canonical_file_paths = data.get("canonical_file_paths")
            assert isinstance(canonical_file_paths, list)
            assert "Dockerfile" in canonical_file_paths

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
        manager: RuleDefinitionManager,
    ) -> RuleEffect:
        _sync_bundled(db)

        row = manager.get_by_name("no-full-vitest-suite")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        return _first_effect(body)

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
        manager: RuleDefinitionManager,
    ) -> RuleEffect:
        _sync_bundled(db)

        row = manager.get_by_name("no-full-cargo-test")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        return _first_effect(body)

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
        manager: RuleDefinitionManager,
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
        manager: RuleDefinitionManager,
        command: str,
    ) -> None:
        effect = self._effect(db, manager)

        assert not self._is_blocked(effect, command)
