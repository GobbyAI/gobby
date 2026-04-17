import pytest

from gobby.agents.spawners.command_builder import (
    build_cli_command,
    build_codex_command_with_resume,
    build_gemini_command_with_resume,
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

    def test_gemini_basic(self):
        cmd, _env = build_cli_command("gemini", prompt="hello")
        assert cmd == ["gemini", "hello"]

    def test_gemini_auto_approve(self):
        cmd, _env = build_cli_command("gemini", auto_approve=True, prompt="hello")
        assert cmd == ["gemini", "--approval-mode", "yolo", "hello"]

    def test_gemini_with_model(self):
        cmd, _env = build_cli_command("gemini", model="gemini-1.5-pro", prompt="hello")
        assert cmd == ["gemini", "--model", "gemini-1.5-pro", "hello"]

    def test_codex_basic(self):
        cmd, _env = build_cli_command("codex", prompt="hello")
        assert cmd == ["codex", "hello"]

    def test_codex_auto_approve(self):
        cmd, _env = build_cli_command("codex", auto_approve=True, prompt="hello")
        assert cmd == ["codex", "--full-auto", "hello"]

    def test_codex_working_directory(self):
        cmd, _env = build_cli_command("codex", working_directory="/tmp", prompt="hello")
        assert cmd == ["codex", "-C", "/tmp", "hello"]

    def test_codex_with_model(self):
        cmd, _env = build_cli_command("codex", model="gpt-4", prompt="hello")
        assert cmd == ["codex", "--model", "gpt-4", "hello"]

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


class TestBuildGeminiResume:
    def test_basic_resume(self):
        cmd = build_gemini_command_with_resume("ext-123")
        assert cmd == ["gemini", "-r", "ext-123"]

    def test_resume_with_prompt(self):
        cmd = build_gemini_command_with_resume("ext-123", prompt="continue")
        assert cmd == ["gemini", "-r", "ext-123", "-i", "continue"]

    def test_resume_auto_approve(self):
        cmd = build_gemini_command_with_resume("ext-123", auto_approve=True)
        assert cmd == ["gemini", "-r", "ext-123", "--approval-mode", "yolo"]

    def test_resume_with_model(self):
        cmd = build_gemini_command_with_resume("ext-123", model="gemini-1.5-pro")
        assert cmd == ["gemini", "-r", "ext-123", "--model", "gemini-1.5-pro"]

    def test_resume_with_gobby_session(self):
        cmd = build_gemini_command_with_resume(
            "ext-123", gobby_session_id="gob-456", prompt="do it"
        )
        assert cmd[0:3] == ["gemini", "-r", "ext-123"]
        assert cmd[3] == "-i"
        assert "Your Gobby session_id is: gob-456" in cmd[4]
        assert "do it" in cmd[4]


class TestBuildCodexResume:
    def test_basic_resume(self):
        cmd = build_codex_command_with_resume("ext-123")
        assert cmd == ["codex", "resume", "ext-123"]

    def test_resume_with_prompt(self):
        cmd = build_codex_command_with_resume("ext-123", prompt="continue")
        assert cmd == ["codex", "resume", "ext-123", "continue"]

    def test_resume_auto_approve(self):
        cmd = build_codex_command_with_resume("ext-123", auto_approve=True)
        assert cmd == ["codex", "resume", "ext-123", "--full-auto"]

    def test_resume_with_model(self):
        cmd = build_codex_command_with_resume("ext-123", model="gpt-4")
        assert cmd == ["codex", "resume", "ext-123", "--model", "gpt-4"]

    def test_resume_with_working_directory(self):
        cmd = build_codex_command_with_resume("ext-123", working_directory="/tmp")
        assert cmd == ["codex", "resume", "ext-123", "-C", "/tmp"]

    def test_resume_with_sandbox_args(self):
        cmd = build_codex_command_with_resume(
            "ext-123",
            sandbox_args=["--sandbox", "workspace-write"],
        )
        assert cmd == ["codex", "resume", "ext-123", "--sandbox", "workspace-write"]

    def test_resume_with_gobby_session(self):
        cmd = build_codex_command_with_resume(
            "ext-123", gobby_session_id="gob-789", prompt="fix it"
        )
        assert cmd[0:3] == ["codex", "resume", "ext-123"]
        assert "Your Gobby session_id is: gob-789" in cmd[3]
        assert "fix it" in cmd[3]
