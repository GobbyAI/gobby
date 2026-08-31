"""Shared inventory for bundled before-tool block-reason framing tests."""

from typing import Any

import yaml

from gobby.workflows.sync_rules import get_bundled_rules_path

REDIRECT_RULES = frozenset(
    {
        "block-ask-during-stop-compliance",
        "block-claude-memory-read",
        "block-claude-memory-search",
        "block-claude-memory-tool",
        "block-claude-memory-write",
        "block-edits-plan-mode",
        "block-git-clone",
        "block-git-clone-interactive",
        "block-git-worktree-mutations",
        "block-git-worktree-mutations-interactive",
        "block-gobby-tasks-cli",
        "block-native-task-tools-unclaimed",
        "block-native-todo-write",
        "block-needs-review-interactive",
        "block-reopen-task",
        "enforce-tdd-block",
        "guard-plan-memory-writes",
        "no-bash-sleep",
        "no-external-github-issues",
        "no-full-cargo-test",
        "no-full-go-test",
        "no-full-pytest-suite",
        "no-full-vitest-suite",
        "no-git-stash",
        "no-git-stash-interactive",
        "no-invalid-git-flags",
        "prefer-gcode-for-code-search",
        "prefer-gcode-for-source-read",
        "require-bash-skill",
        "require-build-coordinator-for-gobby-build",
        "require-c-skill",
        "require-claimed-task-required-skills",
        "require-clean-tree-before-status",
        "require-code-index-skill",
        "require-commit-before-status",
        "require-cpp-skill",
        "require-csharp-skill",
        "require-current-context-schema-before-call",
        "require-dart-skill",
        "require-decompose-monolith-before-threshold-write",
        "require-elixir-skill",
        "require-go-skill",
        "require-impeccable-skill",
        "require-java-skill",
        "require-javascript-skill",
        "require-json-skill",
        "require-kotlin-skill",
        "require-lua-skill",
        "require-monolith-resolution-before-commit",
        "require-monolith-resolution-before-task-transition",
        "require-objc-skill",
        "require-php-skill",
        "require-plan-skill",
        "require-pytest-guard-env",
        "require-python-skill",
        "require-restraint-skill",
        "require-ruby-skill",
        "require-rust-skill",
        "require-scala-skill",
        "require-swift-skill",
        "require-task-before-commit",
        "require-task-before-edit",
        "require-task-creation-skill-loaded",
        "require-task-creation-skill-on-schema",
        "require-task-transitions-skill-loaded",
        "require-task-transitions-skill-on-lifecycle",
        "require-typescript-skill",
        "require-uv",
        "require-yaml-skill",
        "task-commit-project-path-allowlist-before-git",
    }
)

TRUE_RESTRICTION_RULES = frozenset(
    {
        "block-autonomous-clear-session",
        "block-cross-session-foreign-dirty-edit",
        "block-cross-session-foreign-staged-commit",
        "block-docker-policy-edits",
        "block-web-chat-send-keys",
        "no-agent-spawn-for-merge",
        "no-brew-install",
        "no-cargo-add",
        "no-cargo-publish",
        "no-cargo-publish-interactive",
        "no-curl-upload",
        "no-daemon-management",
        "no-daemon-management-http",
        "no-dd",
        "no-dd-interactive",
        "no-destructive-git",
        "no-destructive-git-interactive",
        "no-force-kill",
        "no-force-kill-interactive",
        "no-force-push",
        "no-force-push-interactive",
        "no-gem-install",
        "no-gem-push",
        "no-gem-push-interactive",
        "no-npm-install",
        "no-npm-publish",
        "no-npm-publish-interactive",
        "no-pip-install",
        "no-push",
        "no-push-for-workers",
        "no-recursive-permissions",
        "no-recursive-permissions-interactive",
        "no-recursive-rm",
        "no-recursive-rm-interactive",
        "no-remote-copy",
        "no-remote-exec",
        "no-secret-read",
        "no-secure-delete",
        "no-secure-delete-interactive",
        "no-truncate",
        "no-truncate-interactive",
        "no-twine-upload",
        "no-twine-upload-interactive",
        "no-uv-add",
        "no-wget-upload",
        "no-yarn-add",
        "review-closed-task-memories-before-handoff",
        "review-gobby-session-feedback-before-handoff",
    }
)

SKILL_FETCH_REASON_TEMPLATE = "{{ skill_fetch_batch_directive(missing_claimed_task_required_skills(tool_input, event.data)) }}"


def bundled_before_tool_block_reasons(*, validate: bool = False) -> dict[str, str]:
    reasons: dict[str, str] = {}
    rules_path = get_bundled_rules_path()
    for yaml_file in sorted(rules_path.rglob("*.yaml")):
        if "deprecated" in yaml_file.relative_to(rules_path).parts:
            continue
        data: Any = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        rules: Any = data.get("rules") or {}
        if not isinstance(rules, dict):
            continue
        for rule_name, rule_data in rules.items():
            if not isinstance(rule_name, str) or not isinstance(rule_data, dict):
                continue
            if rule_data.get("event") != "before_tool":
                continue
            raw_effects = rule_data.get("effects")
            if raw_effects is None:
                raw_effect = rule_data.get("effect")
                raw_effects = [raw_effect] if raw_effect is not None else []
            if not isinstance(raw_effects, list):
                if validate:
                    raise AssertionError(f"{rule_name} effects must be a list")
                continue
            block_effects = [
                effect
                for effect in raw_effects
                if isinstance(effect, dict) and effect.get("type") == "block"
            ]
            if not block_effects:
                continue
            if len(block_effects) != 1:
                if validate:
                    raise AssertionError(f"{rule_name} must have one block reason")
                continue
            reason = block_effects[0].get("reason")
            if not isinstance(reason, str):
                if validate:
                    raise AssertionError(f"{rule_name} must have a string block reason")
                continue
            if rule_name in reasons:
                if validate:
                    raise AssertionError(f"duplicate live block rule: {rule_name}")
                continue
            reasons[rule_name] = reason
    return reasons
