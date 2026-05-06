import pytest

from gobby.agents.spawners.command_builder import (
    build_cli_command,
)

pytestmark = pytest.mark.unit


class TestBuildCliCommand:
    def test_claude_basic(self):
        cmd, _env = build_cli_command("claude", prompt="hello")
        assert cmd == ["claude", "hello"]

    def test_claude_with_session_id(self):
        cmd, _env = build_cli_command("claude", session_id="123", prompt="hello")
        assert cmd == ["claude", "--session-id", "123", "hello"]

    def test_claude_auto_approve(self):
        cmd, _env = build_cli_command("claude", auto_approve=True, prompt="hello")
        assert cmd == ["claude", "--dangerously-skip-permissions", "hello"]

    def test_claude_with_model(self):
        cmd, _env = build_cli_command("claude", model="claude-3-opus", prompt="hello")
        assert cmd == ["claude", "--model", "claude-3-opus", "hello"]

    def test_claude_with_reasoning_effort(self):
        cmd, _env = build_cli_command("claude", reasoning_effort="high", prompt="hello")
        assert cmd == ["claude", "--effort", "high", "hello"]

    def test_gemini_basic(self):
        cmd, _env = build_cli_command("gemini", prompt="hello")
        assert cmd == ["gemini", "hello"]

    def test_gemini_auto_approve(self):
        cmd, _env = build_cli_command("gemini", auto_approve=True, prompt="hello")
        assert cmd == ["gemini", "--approval-mode", "yolo", "hello"]

    def test_gemini_with_model(self):
        cmd, _env = build_cli_command("gemini", model="gemini-1.5-pro", prompt="hello")
        assert cmd == ["gemini", "--model", "gemini-1.5-pro", "hello"]

    def test_gemini_reasoning_uses_model_settings_without_extra_flag(self):
        cmd, _env = build_cli_command(
            "gemini",
            model="gemini-3.1-pro-preview",
            reasoning_effort="high",
            prompt="hello",
        )
        assert cmd == ["gemini", "--model", "gemini-3.1-pro-preview", "hello"]

    def test_codex_basic(self):
        cmd, _env = build_cli_command("codex", prompt="hello")
        assert cmd == ["codex", "hello"]

    def test_codex_auto_approve(self):
        cmd, _env = build_cli_command("codex", auto_approve=True, prompt="hello")
        assert cmd == [
            "codex",
            "--ask-for-approval",
            "never",
            "--disable",
            "guardian_approval",
            "hello",
        ]
        assert "--approval-policy" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "--full-auto" not in cmd

    @pytest.mark.parametrize(
        ("cli", "approval_args"),
        [
            ("claude", ["--dangerously-skip-permissions"]),
            ("gemini", ["--approval-mode", "yolo"]),
            ("qwen", ["--approval-mode", "yolo"]),
        ],
    )
    def test_auto_approve_flags_precede_sandbox_args(self, cli, approval_args):
        cmd, _env = build_cli_command(
            cli,
            auto_approve=True,
            prompt="hello",
            sandbox_args=["--sandbox"],
        )
        assert cmd == [cli, *approval_args, "--sandbox", "hello"]

    def test_codex_working_directory(self):
        cmd, _env = build_cli_command("codex", working_directory="/tmp", prompt="hello")
        assert cmd == ["codex", "-C", "/tmp", "hello"]

    def test_codex_with_model(self):
        cmd, _env = build_cli_command("codex", model="gpt-4", prompt="hello")
        assert cmd == ["codex", "--model", "gpt-4", "hello"]

    def test_codex_with_reasoning_effort(self):
        cmd, _env = build_cli_command("codex", reasoning_effort="xhigh", prompt="hello")
        assert cmd == ["codex", "-c", 'model_reasoning_effort="xhigh"', "hello"]

    def test_codex_config_overrides_precede_prompt(self):
        cmd, _env = build_cli_command(
            "codex",
            prompt="hello",
            config_overrides=[
                'mcp_servers.gobby.command="uv"',
                'mcp_servers.gobby.args=["run","--project","/repo","gobby","mcp-server"]',
            ],
        )
        assert cmd == [
            "codex",
            "-c",
            'mcp_servers.gobby.command="uv"',
            "-c",
            'mcp_servers.gobby.args=["run","--project","/repo","gobby","mcp-server"]',
            "hello",
        ]

    def test_droid_agent_command(self):
        cmd, _env = build_cli_command(
            "droid",
            prompt="hello",
            working_directory="/tmp/wt",
            model="claude-opus-4-7",
            reasoning_effort="high",
            auto_approve=True,
        )
        assert cmd == [
            "droid",
            "exec",
            "--input-format",
            "stream-json",
            "--cwd",
            "/tmp/wt",
            "--model",
            "claude-opus-4-7",
            "--reasoning-effort",
            "high",
            "--auto",
            "high",
            "hello",
        ]
        assert "--worktree" not in cmd
        assert "--session-id" not in cmd

    def test_droid_auto_approve_false_uses_low_autonomy(self):
        cmd, _env = build_cli_command("droid", auto_approve=False, prompt="hello")
        assert cmd == [
            "droid",
            "exec",
            "--input-format",
            "stream-json",
            "--auto",
            "low",
            "hello",
        ]

    def test_generic_sandbox_args(self):
        cmd, _env = build_cli_command("claude", prompt="hello", sandbox_args=["--sandbox"])
        # sandbox args come before prompt
        assert cmd == ["claude", "--sandbox", "hello"]

    def test_claude_interactive_mode_uses_stream_json_and_no_prompt(self):
        cmd, env = build_cli_command("claude", prompt="hello", mode="interactive")
        assert cmd == [
            "claude",
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
        ]
        assert env == {}

    def test_gemini_interactive_mode_uses_acp_and_resume(self):
        cmd, env = build_cli_command(
            "gemini",
            prompt="hello",
            mode="interactive",
            session_id="gem-session",
            env_overrides={"CUSTOM_VAR": "value"},
        )
        assert cmd == ["gemini", "--acp", "--resume", "gem-session"]
        assert env == {"CUSTOM_VAR": "value"}
