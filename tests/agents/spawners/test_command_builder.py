import pytest

from gobby.agents.spawners.command_builder import (
    build_cli_command,
)

pytestmark = pytest.mark.unit

# Managed overrides every Codex spawn ends with, after caller overrides.
_CODEX_MANAGED_TAIL = ["-c", "check_for_update_on_startup=false"]


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("claude", ["--effort", "high"]),
        ("agy", ["--effort", "high"]),
        ("codex", ["-c", 'model_reasoning_effort="high"']),
        ("droid", ["--reasoning-effort", "high"]),
        ("grok", ["--reasoning-effort", "high"]),
    ],
)
def test_reasoning_flag_styles_unchanged(provider: str, expected: list[str]) -> None:
    cmd, _env = build_cli_command(provider, reasoning_effort="high", prompt="hello")

    assert expected[0] in cmd
    start = cmd.index(expected[0])
    assert cmd[start : start + len(expected)] == expected


class TestBuildCliCommand:
    def test_claude_basic(self) -> None:
        cmd, _env = build_cli_command("claude", prompt="hello")
        assert cmd == ["claude", "hello"]

    def test_claude_with_session_id(self) -> None:
        cmd, _env = build_cli_command("claude", session_id="123", prompt="hello")
        assert cmd == ["claude", "--session-id", "123", "hello"]

    def test_claude_auto_approve(self) -> None:
        cmd, _env = build_cli_command("claude", auto_approve=True, prompt="hello")
        assert cmd == ["claude", "--dangerously-skip-permissions", "hello"]

    def test_claude_with_model(self) -> None:
        cmd, _env = build_cli_command("claude", model="claude-3-opus", prompt="hello")
        assert cmd == ["claude", "--model", "claude-3-opus", "hello"]

    def test_claude_with_reasoning_effort(self) -> None:
        cmd, _env = build_cli_command("claude", reasoning_effort="high", prompt="hello")
        assert cmd == ["claude", "--effort", "high", "hello"]

    def test_claude_keeps_native_tools(self) -> None:
        cmd, _env = build_cli_command("claude", auto_approve=True, prompt="hello")
        assert cmd == ["claude", "--dangerously-skip-permissions", "hello"]

    def test_unsupported_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported CLI: unsupported"):
            build_cli_command("unsupported", prompt="hello")

    def test_codex_basic(self) -> None:
        cmd, _env = build_cli_command("codex", prompt="hello")
        assert cmd == ["codex", *_CODEX_MANAGED_TAIL, "hello"]

    def test_codex_spawn_keeps_code_mode_host_enabled(self) -> None:
        # gpt-5.6 models are tool_mode=code_mode_only: Codex's code-mode host is
        # their only MCP executor, so a managed `features.code_mode_host=false`
        # makes every Gobby MCP call fail closed. The managed tail must leave the
        # features namespace alone and still follow caller overrides.
        cmd, _env = build_cli_command(
            "codex",
            prompt="hello",
            config_overrides=["features.code_mode_host=true"],
        )

        assert cmd[-len(_CODEX_MANAGED_TAIL) - 1 :] == [*_CODEX_MANAGED_TAIL, "hello"]
        assert not any(arg.startswith("features.") and arg.endswith("=false") for arg in cmd)
        assert cmd.index("check_for_update_on_startup=false") > cmd.index(
            "features.code_mode_host=true"
        )

    def test_codex_auto_approve(self) -> None:
        cmd, _env = build_cli_command("codex", auto_approve=True, prompt="hello")
        assert cmd == [
            "codex",
            "--ask-for-approval",
            "never",
            "--disable",
            "guardian_approval",
            *_CODEX_MANAGED_TAIL,
            "hello",
        ]
        assert "--approval-policy" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "--full-auto" not in cmd

    @pytest.mark.parametrize(
        ("cli", "approval_args"),
        [
            ("claude", ["--dangerously-skip-permissions"]),
            ("qwen", ["--approval-mode", "yolo"]),
        ],
    )
    def test_auto_approve_flags_precede_sandbox_args(self, cli, approval_args) -> None:
        cmd, _env = build_cli_command(
            cli,
            auto_approve=True,
            prompt="hello",
            sandbox_args=["--sandbox"],
        )
        assert cmd == [cli, *approval_args, "--sandbox", "hello"]

    def test_codex_working_directory(self) -> None:
        cmd, _env = build_cli_command("codex", working_directory="/tmp", prompt="hello")
        assert cmd == [
            "codex",
            "-C",
            "/tmp",
            *_CODEX_MANAGED_TAIL,
            "hello",
        ]

    def test_codex_with_model(self) -> None:
        cmd, _env = build_cli_command("codex", model="gpt-4", prompt="hello")
        assert cmd == [
            "codex",
            "--model",
            "gpt-4",
            *_CODEX_MANAGED_TAIL,
            "hello",
        ]

    def test_codex_with_oss_local_provider(self) -> None:
        cmd, _env = build_cli_command(
            "codex",
            model="ollama/qwen3-coder",
            codex_oss_provider="ollama",
            prompt="hello",
        )
        assert cmd == [
            "codex",
            "--oss",
            "--local-provider",
            "ollama",
            "-m",
            "ollama/qwen3-coder",
            *_CODEX_MANAGED_TAIL,
            "hello",
        ]

    def test_codex_with_reasoning_effort(self) -> None:
        cmd, _env = build_cli_command("codex", reasoning_effort="xhigh", prompt="hello")
        assert cmd == [
            "codex",
            "-c",
            'model_reasoning_effort="xhigh"',
            *_CODEX_MANAGED_TAIL,
            "hello",
        ]

    def test_codex_config_overrides_precede_prompt(self) -> None:
        cmd, _env = build_cli_command(
            "codex",
            prompt="hello",
            config_overrides=[
                'mcp_servers.gobby.command="uv"',
                'mcp_servers.gobby.args=["run","--project","/repo","gobby","mcp-server"]',
                "mcp_servers.gobby.startup_timeout_sec=120",
            ],
        )
        assert cmd == [
            "codex",
            "-c",
            'mcp_servers.gobby.command="uv"',
            "-c",
            'mcp_servers.gobby.args=["run","--project","/repo","gobby","mcp-server"]',
            "-c",
            "mcp_servers.gobby.startup_timeout_sec=120",
            *_CODEX_MANAGED_TAIL,
            "hello",
        ]

    def test_codex_forces_startup_update_check_off_after_caller_overrides(self) -> None:
        cmd, _env = build_cli_command(
            "codex",
            prompt="hello",
            config_overrides=["check_for_update_on_startup=true"],
            sandbox_args=["--sandbox"],
        )

        caller_override = cmd.index("check_for_update_on_startup=true")
        safety_override = cmd.index("check_for_update_on_startup=false")
        assert safety_override > caller_override
        assert cmd[safety_override - 1 : safety_override + 3] == [
            "-c",
            "check_for_update_on_startup=false",
            "--sandbox",
            "hello",
        ]

    def test_droid_agent_command(self) -> None:
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
        # Droid 0.219.0 refuses `--input-format` without a matching
        # `--output-format`, and the spawn pane needs neither (#22402).
        assert "--input-format" not in cmd
        assert "--output-format" not in cmd

    def test_agy_agent_command_uses_noninteractive_print(self) -> None:
        cmd, _env = build_cli_command(
            "agy",
            prompt="implement the task",
            auto_approve=True,
            working_directory="/repo",
            model="gemini-2.5-flash",
            reasoning_effort="high",
            sandbox_args=["--sandbox=false"],
        )
        assert cmd == [
            "agy",
            "--dangerously-skip-permissions",
            "--add-dir",
            "/repo",
            "--model",
            "gemini-2.5-flash",
            "--effort",
            "high",
            "--print",
            "implement the task",
            "--sandbox=false",
        ]
        assert "--mode" not in cmd
        assert "-p" not in cmd
        # Run ff01920d stalled at the TUI banner with this interactive form.
        assert "--prompt-interactive" not in cmd
        assert "--input-format" not in cmd

    def test_agy_resume_uses_conversation_and_add_dir(self) -> None:
        cmd, _env = build_cli_command(
            "agy",
            prompt="continue",
            resume_session_id="conv-123",
            auto_approve=True,
            working_directory="/repo",
        )
        assert "--conversation" in cmd
        assert "--add-dir" in cmd
        conversation_at = cmd.index("--conversation")
        add_dir_at = cmd.index("--add-dir")
        assert cmd[conversation_at : conversation_at + 2] == ["--conversation", "conv-123"]
        assert cmd[add_dir_at : add_dir_at + 2] == ["--add-dir", "/repo"]
        assert "--mode" not in cmd

    def test_agy_does_not_add_mode_flag(self) -> None:
        cmd, _env = build_cli_command(
            "agy",
            prompt="plan this",
            auto_approve=True,
            working_directory="/repo",
            mode="agent",
        )
        assert "--add-dir" in cmd
        assert "--mode" not in cmd
        assert cmd[0] == "agy"

    def test_droid_auto_approve_false_uses_low_autonomy(self) -> None:
        cmd, _env = build_cli_command("droid", auto_approve=False, prompt="hello")
        assert cmd == [
            "droid",
            "exec",
            "--auto",
            "low",
            "hello",
        ]

    def test_generic_sandbox_args(self) -> None:
        cmd, _env = build_cli_command("claude", prompt="hello", sandbox_args=["--sandbox"])
        # sandbox args come before prompt
        assert cmd == ["claude", "--sandbox", "hello"]

    def test_claude_interactive_mode_uses_stream_json_and_no_prompt(self) -> None:
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

    def test_grok_agent_command(self) -> None:
        cmd, _env = build_cli_command(
            "grok",
            prompt="hello",
            working_directory="/tmp/wt",
            model="grok-build",
            reasoning_effort="high",
            auto_approve=True,
        )
        assert cmd == [
            "grok",
            "--always-approve",
            "--no-alt-screen",
            "--cwd",
            "/tmp/wt",
            "--model",
            "grok-build",
            "--reasoning-effort",
            "high",
            "--single",
            "hello",
        ]

    def test_grok_interactive_mode_uses_acp_stdio(self) -> None:
        cmd, _env = build_cli_command(
            "grok",
            mode="interactive",
            model="grok-build",
            reasoning_effort="medium",
        )
        assert cmd == [
            "grok",
            "agent",
            "--no-leader",
            "--always-approve",
            "--model",
            "grok-build",
            "--reasoning-effort",
            "medium",
            "stdio",
        ]

    @pytest.mark.parametrize(
        ("cli", "expected"),
        [
            (
                "claude",
                [
                    "claude",
                    "--resume",
                    "native-123",
                    "--model",
                    "model-x",
                    "--effort",
                    "high",
                    "--dangerously-skip-permissions",
                    "--sandbox",
                    "continue",
                ],
            ),
            (
                "qwen",
                [
                    "qwen",
                    "--model",
                    "model-x",
                    "--approval-mode",
                    "yolo",
                    "--resume",
                    "native-123",
                    "--sandbox",
                    "continue",
                ],
            ),
            (
                "grok",
                [
                    "grok",
                    "--always-approve",
                    "--no-alt-screen",
                    "--cwd",
                    "/tmp/wt",
                    "--model",
                    "model-x",
                    "--reasoning-effort",
                    "high",
                    "--resume",
                    "native-123",
                    "--sandbox",
                    "--single",
                    "continue",
                ],
            ),
            (
                "codex",
                [
                    "codex",
                    "resume",
                    "--model",
                    "model-x",
                    "-c",
                    'model_reasoning_effort="high"',
                    "--ask-for-approval",
                    "never",
                    "--disable",
                    "guardian_approval",
                    "-C",
                    "/tmp/wt",
                    "-c",
                    'mcp_servers.gobby.command="uv"',
                    *_CODEX_MANAGED_TAIL,
                    "--sandbox",
                    "native-123",
                    "continue",
                ],
            ),
            (
                "droid",
                [
                    "droid",
                    "exec",
                    "--session-id",
                    "native-123",
                    "--cwd",
                    "/tmp/wt",
                    "--model",
                    "model-x",
                    "--reasoning-effort",
                    "high",
                    "--auto",
                    "high",
                    "--sandbox",
                    "continue",
                ],
            ),
            (
                "agy",
                [
                    "agy",
                    "--conversation",
                    "native-123",
                    "--dangerously-skip-permissions",
                    "--add-dir",
                    "/tmp/wt",
                    "--model",
                    "model-x",
                    "--effort",
                    "high",
                    "--print",
                    "continue",
                    "--sandbox",
                ],
            ),
        ],
    )
    def test_resume_commands_preserve_launch_settings(self, cli: str, expected: list[str]) -> None:
        cmd, _env = build_cli_command(
            cli,
            prompt="continue",
            resume_session_id="native-123",
            auto_approve=True,
            working_directory="/tmp/wt",
            sandbox_args=["--sandbox"],
            model="model-x",
            reasoning_effort="high",
            config_overrides=['mcp_servers.gobby.command="uv"'],
        )
        assert cmd == expected
