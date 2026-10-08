"""Focused tests for provider-specific spawn configuration."""

from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides


def test_codex_shell_tmpdir_points_at_sandbox_scratchpad() -> None:
    scratchpad = "/Users/dev/.gobby/run/sandbox/run-1/tmp"

    overrides = _codex_mcp_config_overrides("/repo", scratchpad)

    assert f'shell_environment_policy.set.TMPDIR="{scratchpad}"' in overrides
    assert f'shell_environment_policy.set.TMPPREFIX="{scratchpad}/zsh"' in overrides
    assert 'shell_environment_policy.set.OCR_NO_UPDATE="1"' in overrides


def test_codex_shell_tmpdir_is_omitted_without_sandbox() -> None:
    overrides = _codex_mcp_config_overrides("/repo", None)

    assert not any(entry.startswith("shell_environment_policy.set.TMPDIR") for entry in overrides)


def test_codex_shell_and_mcp_use_provisioned_cache_paths() -> None:
    import tomllib

    cache_env = {
        "UV_CACHE_DIR": "/sandbox/cache/uv",
        "CARGO_HOME": "/sandbox/shared/cargo-home",
        "CARGO_TARGET_DIR": "/sandbox/checkout/cargo-target",
    }
    config = tomllib.loads(
        "\n".join(_codex_mcp_config_overrides("/repo", managed_identity_env=cache_env))
    )

    for name, path in cache_env.items():
        assert config["shell_environment_policy"]["set"][name] == path
        assert config["mcp_servers"]["gobby"]["env"][name] == path


def test_managed_codex_requires_gobby_before_first_turn() -> None:
    import tomllib

    config = tomllib.loads("\n".join(_codex_mcp_config_overrides("/main/repo")))
    server = config["mcp_servers"]["gobby"]

    assert server["required"] is True
    assert server["args"] == ["run", "--no-sync", "--project", "/main/repo", "gobby", "mcp-server"]


def test_resume_retains_required_gobby_server() -> None:
    import tomllib

    from gobby.agents.resume_metadata import filter_resume_config_overrides

    restored = filter_resume_config_overrides(_codex_mcp_config_overrides("/main/repo"))
    config = tomllib.loads("\n".join(restored))

    assert config["mcp_servers"]["gobby"]["required"] is True
    assert "--no-sync" in config["mcp_servers"]["gobby"]["args"]


def test_codex_close_reviewer_resume_keeps_execution_wrappers_disabled() -> None:
    from gobby.agents.resume_metadata import filter_resume_config_overrides

    overrides = [
        'plugins."unified-computer-use@openai-bundled".enabled=false',
        "mcp_servers.node_repl.enabled=false",
    ]

    assert filter_resume_config_overrides(overrides) == overrides
