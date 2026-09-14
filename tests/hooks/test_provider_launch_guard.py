"""Literal launch classification and real-engine enforcement without launching providers."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.provider_launch_guard import blocks_direct_provider_launch
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_rule_file

pytestmark = pytest.mark.unit
SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
RULE = SHARED / "workflows/rules/worker-safety/block-direct-provider-launch.yaml"
SESSION = "abababab-0000-4000-8000-000000000001"


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


@pytest.mark.parametrize("provider", ["codex", "claude", "droid", "grok", "qwen", "agy"])
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
        "qwen --version",
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
        "qwen mcp --help",
        "agy help mcp",
        "claude auth status",
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
        "qwen -v",
        "type codex",
        "which codex",
        "cat <<'EOF'\ncodex exec\n$(claude -p hi)\n' malformed documentation\nEOF\n",
        "cat <<EOF\ncodex exec\nEOF\n",
        "cat <<EOF\n<(codex)\nEOF\n",
        "bash script.sh <<'EOF'\ncodex exec\nEOF\n",
        "uv run python -c 'import subprocess; subprocess.run([\"codex\"])'",
        "cat <<'EOF' |\n tee docs\n$(codex exec)\nEOF\n",
        "echo hello | sh",
        "bash -c 'printf hello' <<'EOF'\ncodex\nEOF\n",
        "echo '# codex'; printf '%s' '# claude'",
        "echo \"$(printf '%s' 'codex exec')\"",
        "git commit -m \"$(cat <<'EOF'\nfix: the watchdog's hook (1M context\nEOF\n)\"",
        'git commit -m "$(cat <<EOF\nit\'s done\nEOF\n)"',
        "x=\"$(cat <<-'EOF' |\n tr a b\n\tit's (text\n\tEOF\n)\"",
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
        "qwen help mcp",
        "grok agent prompt --help",
        "agy mcp unknown --help",
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
        "true & qwen",
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
        "/usr/bin/env -- codex",
        "time -p codex",
        "env -S 'codex exec'",
        "env --split-string='codex exec'",
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
        "bash <<-'EOF'\n\tqwen\n\tEOF\n",
        "cat <<EOF\n'$(codex exec hi)'\nEOF\n",
        "cat <<EOF\n`claude -p hi`\nEOF\n",
        "cat <<'EOF' | sh\ncodex exec\nEOF\n",
        "cat <<'EOF' |\n sh\ncodex\nEOF\n",
        "sh <<< 'codex exec'",
        "printf '%s' 'codex exec' | sh",
        "codex --help; codex exec hi",
        "codex login status --unknown",
        "claude auth login",
        "droid auth status",
        "grok auth status",
        "qwen auth status",
        "agy auth status",
        "qwen help",
        "qwen -V",
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
    ],
)
def test_execution_contexts(command: str) -> None:
    assert blocks_direct_provider_launch("Bash", {"command": command})


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


def test_bounded_and_malformed_input() -> None:
    assert blocks_direct_provider_launch("Bash", {"command": "echo " + "x" * 131_072})
    assert blocks_direct_provider_launch("Bash", {"command": "echo 'unclosed"})
    assert blocks_direct_provider_launch("Bash", {"command": "echo \"$(cat <<'EOF'\nit's\n)\""})
    assert blocks_direct_provider_launch(
        "Bash", {"command": "echo " + "$(" * 30 + "codex" + ")" * 30}
    )
    assert not blocks_direct_provider_launch("spawn_agent", {"command": "codex exec hi"})


@pytest.mark.parametrize("agent", ["default", "backend-developer", "merge-worker"])
@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
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
