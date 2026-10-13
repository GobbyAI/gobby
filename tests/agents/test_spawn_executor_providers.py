import json
import logging
import shutil
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import spawn_executor_codex
from gobby.agents.claude_sandbox_home import prepare_claude_reviewer_home
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import (
    ProviderSpawnPlan,
    _prepare_provider_sandbox,
    prepare_claude_spawn,
    prepare_codex_spawn,
    prepare_grok_spawn,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError, render_srt_settings
from gobby.sessions.transcript_paths import TranscriptCallerContext, find_transcript_on_disk
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
)
from tests.agents.prepared_spawn import prepared_spawn


def _sandbox_request() -> tuple[SpawnRequest, PreparedSpawn, MagicMock]:
    run_manager = MagicMock()
    spawn_context = prepared_spawn(
        session_id="child",
        agent_run_id="actual-run",
        env_vars={"GOBBY_SESSION_ID": "child"},
    )
    request = SpawnRequest(
        prompt="work",
        cwd="/workspace",
        provider="codex",
        session_id="parent",
        run_id="requested-run",
        parent_session_id="parent",
        project_id="project",
        run_manager=run_manager,
        sandbox_config=SandboxConfig(enabled=True, backend="srt"),
        prepared_spawn=spawn_context,
    )
    return request, spawn_context, run_manager


@pytest.mark.parametrize("execution_mode", ["one_shot", "interactive"])
@pytest.mark.asyncio
async def test_grok_spawn_preserves_standing_seat_lifetime(
    monkeypatch: pytest.MonkeyPatch, execution_mode: str
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        provider="grok",
        agent_name="security-reviewer",
        model="grok-4.7",
        effective_reasoning_effort="xhigh",
        session_manager=MagicMock(),
        resume_metadata_json={"execution_mode": execution_mode},
    )
    sandbox = AsyncMock(return_value=SandboxLaunch(backend="srt", enforced=True))
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers._prepare_provider_sandbox", sandbox)
    record_launch = MagicMock()
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", record_launch
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())

    plan = await prepare_grok_spawn(request)

    assert isinstance(plan, ProviderSpawnPlan)
    expected = [
        "grok",
        "--always-approve",
        "--no-alt-screen",
        "--cwd",
        "/workspace",
        "--model",
        "grok-4.7",
        "--reasoning-effort",
        "xhigh",
    ]
    if execution_mode == "one_shot":
        expected.append("--single")
    assert plan.command == [*expected, "work"]
    assert plan.launch.enforced
    assert plan.env["GOBBY_SESSION_ID"] == "child"
    sandbox.assert_awaited_once()
    assert record_launch.call_args.kwargs["provider_cli_mode"] == (
        "terminal" if execution_mode == "interactive" else "agent"
    )


@pytest.mark.parametrize("agent_name", ["task-close-reviewer", "backend-developer"])
@pytest.mark.parametrize(
    ("backend", "enforced"),
    [("srt", True), ("srt", False), ("provider-native", True)],
)
@pytest.mark.asyncio
async def test_claude_srt_close_reviewer_disables_housekeeping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_name: str, backend: str, enforced: bool
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "host-home")
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        provider="claude",
        agent_name=agent_name,
        session_manager=MagicMock(),
        extra_env={
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "0",
            "XDG_CACHE_HOME": str(tmp_path / "run-cache"),
        },
    )
    launch = SandboxLaunch(backend=backend, enforced=enforced)
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=launch),
    )
    record_launch = MagicMock()
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", record_launch
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())

    plan = await prepare_claude_spawn(request)

    assert isinstance(plan, ProviderSpawnPlan)
    quiet = agent_name == "task-close-reviewer" and backend == "srt" and enforced
    assert plan.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == ("1" if quiet else "")
    assert plan.env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] == (
        "1" if quiet else "0"
    )
    assert record_launch.call_args.kwargs["env"] == plan.env
    assert plan.launch is launch


@pytest.mark.parametrize("configured_home", [False, True])
@pytest.mark.parametrize("secure_storage", [None, "", "/explicit-auth-home"])
def test_claude_reviewer_state_preserves_auth_and_hooks_without_copying_account_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_home: bool,
    secure_storage: str | None,
) -> None:
    home = tmp_path / "host-home"
    monkeypatch.setattr(Path, "home", lambda: home)
    host_config = home / ("alternate" if configured_home else ".claude")
    host_config.mkdir(parents=True)
    settings = host_config / "settings.json"
    settings.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    settings.chmod(0o400)
    credentials = host_config / ".credentials.json"
    credentials.write_text("host-only-credential-sentinel", encoding="utf-8")
    host_global = home / ".claude.json"
    host_global.write_text('{"host_only": true}', encoding="utf-8")
    cache = tmp_path / "run" / "cache" / "xdg-cache-home"
    env = {"XDG_CACHE_HOME": str(cache)}
    if configured_home:
        env["CLAUDE_CONFIG_DIR"] = str(host_config)
    if secure_storage is not None:
        env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = secure_storage
    request, _, _ = _sandbox_request()
    request = replace(request, cwd="/workspace", auto_approve=True)

    result = prepare_claude_reviewer_home(cache, env, workspace=request.cwd, auto_approve=True)
    env.update(result.environment)

    expected_auth = secure_storage
    if expected_auth is None:
        expected_auth = str(host_config) if configured_home else ""
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == expected_auth
    config = Path(env["CLAUDE_CONFIG_DIR"])
    assert config == cache / "claude"
    assert Path(env["XDG_STATE_HOME"]).is_relative_to(cache)
    assert not (config / "settings.json").is_symlink()
    assert str(config / "settings.json") in result.protected_write_paths
    assert (config / "settings.json").read_text(encoding="utf-8") == settings.read_text(
        encoding="utf-8"
    )
    assert json.loads((config / ".claude.json").read_text(encoding="utf-8")) == {
        "hasCompletedOnboarding": True,
        "bypassPermissionsModeAccepted": True,
        "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
    }
    assert (config / "projects" / "-workspace").is_dir()
    assert (config / "projects" / "-workspace").resolve() == (
        home / ".claude" / "projects" / "-workspace"
    )
    assert (config / ".claude.json").stat().st_mode & 0o777 == 0o600
    assert not (config / ".credentials.json").exists()
    assert credentials.read_text(encoding="utf-8") == "host-only-credential-sentinel"
    assert host_global.read_text(encoding="utf-8") == '{"host_only": true}'


@pytest.mark.parametrize(
    "workspace,encoded", [("/workspace", "-workspace"), ("/my_workspace dir", "-my-workspace-dir")]
)
@pytest.mark.parametrize("configured_home", [False, True])
def test_claude_reviewer_state_keeps_only_mcp_scopes_and_current_transcripts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_home: bool,
    workspace: str,
    encoded: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    host_config = home / ".claude"
    host_config.mkdir(parents=True)
    env: dict[str, str] = {}
    if configured_home:
        env["CLAUDE_CONFIG_DIR"] = str(host_config)
    global_config = host_config / ".claude.json" if configured_home else home / ".claude.json"
    user_server = {"command": "gobby", "env": {"DUMMY_TOKEN": "fixture-only"}}
    project_server = {"command": "gobby", "args": ["mcp-server", "--project", "/workspace"]}
    global_config.write_text(
        json.dumps(
            {
                "oauthAccount": {"dummy": "must-not-copy"},
                "mcpServers": {"gobby": user_server},
                "projects": {
                    workspace: {"mcpServers": {"gobby": project_server}, "other": "drop"},
                    "/other": {"mcpServers": {"foreign": user_server}},
                },
            }
        ),
        encoding="utf-8",
    )
    other_project = host_config / "projects" / "-other"
    other_project.mkdir(parents=True)
    cache = tmp_path / "cache"

    result = prepare_claude_reviewer_home(cache, env, workspace=workspace, auto_approve=True)

    private = Path(result.environment["CLAUDE_CONFIG_DIR"])
    seed = json.loads((private / ".claude.json").read_text(encoding="utf-8"))
    assert seed == {
        "hasCompletedOnboarding": True,
        "bypassPermissionsModeAccepted": True,
        "mcpServers": {"gobby": user_server},
        "projects": {
            workspace: {
                "mcpServers": {"gobby": project_server},
                "hasTrustDialogAccepted": True,
            }
        },
    }
    assert not (private / "projects" / "-other").exists()
    transcript = private / "projects" / encoded / "review.jsonl"
    transcript.write_text("review evidence", encoding="utf-8")
    assert (host_config / "projects" / encoded / "review.jsonl").read_text(
        encoding="utf-8"
    ) == "review evidence"
    assert (private / ".claude.json").stat().st_mode & 0o777 == 0o600
    shutil.rmtree(cache)
    contexts: tuple[TranscriptCallerContext, ...] = ("hook", "recovery")
    for caller_context in contexts:
        assert find_transcript_on_disk(
            "claude",
            "review",
            owner_machine_id="local",
            local_machine_id="local",
            cwd=workspace,
            caller_context=caller_context,
        ) == str(host_config / "projects" / encoded / "review.jsonl")


@pytest.mark.asyncio
async def test_claude_reviewer_spawn_preserves_user_mcp_without_workspace_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = {"command": "gobby", "args": ["mcp-server"]}
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"gobby": server}}), encoding="utf-8"
    )
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        cwd=str(workspace),
        provider="claude",
        agent_name="task-close-reviewer",
        session_manager=MagicMock(),
    )

    async def prepare(**kwargs: object) -> SandboxLaunch:
        assert kwargs["claude_reviewer_auto_approve"] is request.auto_approve
        state = prepare_claude_reviewer_home(
            tmp_path / "cache", {}, workspace=request.cwd, auto_approve=request.auto_approve
        )
        return SandboxLaunch(backend="srt", enforced=True, provider_env=state.environment)

    monkeypatch.setattr("gobby.agents.spawn_executor_providers.prepare_sandbox_launch", prepare)
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_actual_sandbox_enforcement", MagicMock()
    )
    record = MagicMock()
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", record
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())

    plan = await prepare_claude_spawn(request)

    assert isinstance(plan, ProviderSpawnPlan)
    assert "--mcp-config" not in plan.command
    private = Path(plan.env["CLAUDE_CONFIG_DIR"])
    assert json.loads((private / ".claude.json").read_text(encoding="utf-8"))["mcpServers"] == {
        "gobby": server
    }
    assert record.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(private)


@pytest.mark.parametrize("configured_home", [False, True])
def test_claude_reviewer_user_headers_helper_keeps_original_config_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured_home: bool
) -> None:
    home = tmp_path / "host's home"
    host = home / ("alternate config" if configured_home else ".claude")
    host.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for directory, value in ((host, "user-fixture"), (workspace, "local-fixture")):
        (directory / "headers.json").write_text(json.dumps({"X-Fixture": value}), encoding="utf-8")
        script = directory / "headers.sh"
        script.write_text("#!/bin/sh\ncat headers.json\n", encoding="utf-8")
        script.chmod(0o700)
    env = {"CLAUDE_CONFIG_DIR": str(host)} if configured_home else {}
    global_config = host / ".claude.json" if configured_home else home / ".claude.json"
    helper = {"type": "http", "url": "https://fixture.invalid", "headersHelper": "./headers.sh"}
    global_config.write_text(
        json.dumps(
            {
                "mcpServers": {"user": helper, "empty": {"headersHelper": ""}},
                "projects": {str(workspace): {"mcpServers": {"local": helper}}},
            }
        ),
        encoding="utf-8",
    )

    state = prepare_claude_reviewer_home(
        tmp_path / "cache", env, workspace=str(workspace), auto_approve=True
    )

    private = Path(state.environment["CLAUDE_CONFIG_DIR"])
    seed = json.loads((private / ".claude.json").read_text(encoding="utf-8"))
    user_command = seed["mcpServers"]["user"]["headersHelper"]
    local_command = seed["projects"][str(workspace)]["mcpServers"]["local"]["headersHelper"]
    assert local_command == "./headers.sh"
    assert seed["mcpServers"]["empty"]["headersHelper"] == ""
    for command, cwd, value in (
        (user_command, private, "user-fixture"),
        (local_command, workspace, "local-fixture"),
    ):
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert json.loads(result.stdout) == {"X-Fixture": value}


@pytest.mark.parametrize("linked_path", ["settings", "global", "projects", "transcript"])
def test_claude_reviewer_state_rejects_host_redirections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, linked_path: str
) -> None:
    home = tmp_path / "home"
    host = home / ".claude"
    host.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel.json"
    sentinel.write_text("{}", encoding="utf-8")
    if linked_path == "settings":
        (host / "settings.json").symlink_to(sentinel)
    elif linked_path == "global":
        (home / ".claude.json").symlink_to(sentinel)
    elif linked_path == "projects":
        (host / "projects").symlink_to(foreign, target_is_directory=True)
    else:
        (host / "projects").mkdir()
        (host / "projects" / "-workspace").symlink_to(foreign, target_is_directory=True)

    with pytest.raises(ValueError, match="Claude"):
        prepare_claude_reviewer_home(
            tmp_path / "cache", {}, workspace="/workspace", auto_approve=True
        )

    assert list(foreign.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_claude_reviewer_bad_state_fails_run_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (home / ".claude.json").write_text("invalid-json", encoding="utf-8")
    request, context, run_manager = _sandbox_request()
    request = replace(request, provider="claude", agent_name="task-close-reviewer")

    async def prepare(**kwargs: object) -> SandboxLaunch:
        assert kwargs["claude_reviewer_auto_approve"] is request.auto_approve
        prepare_claude_reviewer_home(
            tmp_path / "cache", {}, workspace=request.cwd, auto_approve=request.auto_approve
        )
        raise AssertionError("invalid config must fail before a launch exists")

    monkeypatch.setattr("gobby.agents.spawn_executor_providers.prepare_sandbox_launch", prepare)
    result = await _prepare_provider_sandbox(request, context, "claude", {})

    assert isinstance(result, SpawnResult)
    assert result.success is False
    assert result.error is not None and "JSONDecodeError" in result.error
    run_manager.fail.assert_called_once_with(context.agent_run_id, result.error)


def test_claude_reviewer_state_refuses_redirected_config_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "host-home")
    cache = tmp_path / "run-cache"
    cache.mkdir()
    foreign = tmp_path / "foreign-state"
    foreign.mkdir()
    protected = foreign / ".claude.json"
    protected.write_text("unchanged", encoding="utf-8")
    (cache / "claude").symlink_to(foreign, target_is_directory=True)
    request, _, _ = _sandbox_request()
    env = {"XDG_CACHE_HOME": str(cache)}

    with pytest.raises(FileExistsError):
        prepare_claude_reviewer_home(cache, env, workspace=request.cwd, auto_approve=True)

    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert "CLAUDE_CONFIG_DIR" not in env


def test_claude_reviewer_state_does_not_seed_unapproved_workspace_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "host-home")
    request, _, _ = _sandbox_request()
    request = replace(request, auto_approve=False)
    env = {"XDG_CACHE_HOME": str(tmp_path / "run-cache")}

    result = prepare_claude_reviewer_home(
        Path(env["XDG_CACHE_HOME"]), env, workspace=request.cwd, auto_approve=False
    )
    env.update(result.environment)

    config = Path(env["CLAUDE_CONFIG_DIR"])
    assert not (config / "projects").exists()
    assert (
        json.loads((config / ".claude.json").read_text(encoding="utf-8"))[
            "bypassPermissionsModeAccepted"
        ]
        is False
    )


@pytest.mark.parametrize(
    ("declared_store", "lookup_error"),
    [
        (True, None),
        (False, None),
        (True, CheckoutNotFoundError),
        (True, CheckoutSentinelRejectedError),
        (True, MissingMachineContextError),
    ],
)
@pytest.mark.asyncio
async def test_close_reviewer_policy_grants_only_declared_main_nextest_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_store: bool,
    lookup_error: type[ValueError] | None,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    main = tmp_path / "main"
    lane = tmp_path / "lane"
    for root, grants in (
        (main, [".gobby/tmp", ".nextest"] if declared_store else [".gobby/tmp"]),
        (lane, [".gobby/tmp", ".nextest"]),
    ):
        (root / ".gobby").mkdir(parents=True)
        (root / ".gobby/project.json").write_text(
            json.dumps({"id": "project", "sandbox": {"extra_write_paths": grants}})
        )
    request, spawn_context, _ = _sandbox_request()
    manager = MagicMock()
    request.session_manager = manager
    request.machine_id = "machine"
    request.project_path = str(lane)
    request.cwd = str(lane)
    request.agent_name = "task-close-reviewer"
    request.resume_metadata_json = {}
    lookup = MagicMock(
        return_value=str(main),
        side_effect=lookup_error("Primary checkout unavailable") if lookup_error else None,
    )
    monkeypatch.setattr("gobby.storage.project_checkouts.require_root", lookup)
    captured_writes: list[str] = []

    async def launch(
        *, config: SandboxConfig, workspace_path: str, env: dict[str, str], **_kwargs: object
    ) -> SandboxLaunch:
        paths = await compute_sandbox_paths(config, workspace_path, provider="codex", env=env)
        settings = render_srt_settings(paths)
        captured_writes.extend(settings["filesystem"]["allowWrite"])
        return SandboxLaunch(backend="srt", enforced=True)

    monkeypatch.setattr("gobby.agents.spawn_executor_providers.prepare_sandbox_launch", launch)
    result = await _prepare_provider_sandbox(request, spawn_context, "codex", {})

    assert isinstance(result, SandboxLaunch)
    lookup.assert_called_once_with(manager._storage.db, "project", "machine")
    expected_store = declared_store and lookup_error is None
    assert (str(main / ".nextest") in captured_writes) is expected_store
    assert str(main) not in captured_writes
    assert str(main / ".gobby/tmp") not in captured_writes
    assert str(lane / ".nextest") in captured_writes
    persisted = SandboxConfig.model_validate(request.resume_metadata_json["sandbox_config"])
    assert (str(main / ".nextest") in persisted.extra_write_paths) is expected_store


@pytest.mark.asyncio
async def test_codex_spawn_uses_sandbox_effective_cache_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, spawn_context, _ = _sandbox_request()
    cache_env = {
        "UV_CACHE_DIR": "/sandbox/cache/uv",
        "CARGO_HOME": "/sandbox/shared/cargo-home",
        "CARGO_TARGET_DIR": "/sandbox/checkout/cargo-target",
    }
    spawn_context.env_vars.update({name: f"/operator/{name}" for name in cache_env})
    request = replace(request, project_path="/main/repo", session_manager=MagicMock())
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=SandboxLaunch(backend="srt", enforced=True, provider_env=cache_env)),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", MagicMock()
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())
    build_command = MagicMock(return_value=(["codex"], {}))
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.build_cli_command", build_command)

    result = await prepare_codex_spawn(request)

    assert isinstance(result, ProviderSpawnPlan)
    config = tomllib.loads("\n".join(build_command.call_args.kwargs["config_overrides"]))
    for name, path in cache_env.items():
        assert config["shell_environment_policy"]["set"][name] == path
        assert config["mcp_servers"]["gobby"]["env"][name] == path


@pytest.mark.parametrize(
    ("agent_name", "plugin_disabled"),
    [("task-close-reviewer", True), ("backend-developer", False)],
)
@pytest.mark.asyncio
async def test_codex_close_reviewer_launch_hides_execution_wrappers(
    monkeypatch: pytest.MonkeyPatch, agent_name: str, plugin_disabled: bool
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        agent_name=agent_name,
        project_path="/main/repo",
        session_manager=MagicMock(),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=SandboxLaunch(backend="provider-native", enforced=False)),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", MagicMock()
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())
    build_command = MagicMock(return_value=(["codex"], {}))
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.build_cli_command", build_command)

    await prepare_codex_spawn(request)

    overrides = build_command.call_args.kwargs["config_overrides"]
    assert "mcp_servers.gobby.required=true" in overrides
    assert ("features.plugins=false" in overrides) is plugin_disabled
    assert ("features.remote_plugin=false" in overrides) is plugin_disabled
    assert (
        'plugins."unified-computer-use@openai-bundled".enabled=false' in overrides
    ) is plugin_disabled
    assert ("mcp_servers.node_repl.enabled=false" in overrides) is plugin_disabled


@pytest.mark.asyncio
async def test_srt_codex_close_reviewer_launches_headless_with_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(
        request,
        agent_name="task-close-reviewer",
        prompt="Review task close",
        session_manager=MagicMock(),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_managed_code_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._prepare_provider_sandbox",
        AsyncMock(return_value=SandboxLaunch(backend="srt", enforced=True)),
    )
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers._record_resume_launch_details", MagicMock()
    )
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.pre_approve_directory", MagicMock())
    build_command = MagicMock(return_value=(["codex", "exec"], {}))
    monkeypatch.setattr("gobby.agents.spawn_executor_providers.build_cli_command", build_command)

    plan = await prepare_codex_spawn(request)

    assert not isinstance(plan, SpawnResult)
    assert plan.command == ["codex", "exec"]
    assert plan.codex_prompt is None
    assert build_command.call_args.kwargs["mode"] == "headless"
    assert build_command.call_args.kwargs["prompt"] == "Review task close"
    assert 'sandbox_mode="danger-full-access"' in build_command.call_args.kwargs["config_overrides"]
    assert build_command.call_args.kwargs["external_sandbox_enforced"] is True


@pytest.mark.asyncio
async def test_headless_codex_persona_is_marked_before_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _ = _sandbox_request()
    request = replace(request, session_manager=MagicMock())
    plan = ProviderSpawnPlan(
        command=["codex", "exec", "Review"],
        env={},
        launch=SandboxLaunch(backend="srt", enforced=True),
        auth_cli="codex",
        child_session_id="child",
        agent_run_id="actual-run",
        codex_prompt=None,
        inject_persona=True,
    )
    events: list[tuple[str, object]] = []

    def mark_persona(child_session_id: str, values: dict[str, bool]) -> None:
        events.append(("mark", (child_session_id, values)))

    monkeypatch.setattr(
        "gobby.workflows.state_manager.SessionVariableManager",
        lambda _db: SimpleNamespace(merge_variables=mark_persona),
    )
    monkeypatch.setattr(spawn_executor_codex, "prepare_codex_spawn", AsyncMock(return_value=plan))
    result = SpawnResult(
        success=True, run_id="actual-run", child_session_id="child", status="running"
    )

    async def runtime_spawn(_request: SpawnRequest, _plan: ProviderSpawnPlan) -> SpawnResult:
        events.append(("spawn", (_request, _plan)))
        return result

    monkeypatch.setattr("gobby.agents.spawn_executor._runtime_spawn", runtime_spawn)

    spawned = await spawn_executor_codex._spawn_codex_terminal(request)
    assert spawned is result
    assert spawned.success is True
    assert events == [
        ("mark", ("child", {"_agent_context_injected": True})),
        ("spawn", (request, plan)),
    ]


@pytest.mark.parametrize(
    ("exception", "expected_detail"),
    [
        (TimeoutError(), "TimeoutError"),
        (
            SrtRuntimeError("managed SRT preflight timed out"),
            "SrtRuntimeError: managed SRT preflight timed out",
        ),
    ],
)
@pytest.mark.asyncio
async def test_sandbox_failure_names_exception_class(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected_detail: str,
) -> None:
    request, spawn_context, run_manager = _sandbox_request()

    async def fail_sandbox_launch(**_kwargs: object) -> None:
        raise exception

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
        fail_sandbox_launch,
    )

    result = await _prepare_provider_sandbox(request, spawn_context, "codex", {})

    assert isinstance(result, SpawnResult)
    expected_error = f"Sandbox startup failed closed for codex: {expected_detail}"
    assert result.error == expected_error
    run_manager.fail.assert_called_once_with("actual-run", expected_error)


@pytest.mark.asyncio
async def test_sandbox_failure_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request, spawn_context, _run_manager = _sandbox_request()
    exception = TimeoutError()

    async def fail_sandbox_launch(**_kwargs: object) -> None:
        raise exception

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
        fail_sandbox_launch,
    )
    caplog.set_level(logging.WARNING, logger="gobby.agents.spawn_executor_providers")

    result = await _prepare_provider_sandbox(request, spawn_context, "codex", {})

    assert isinstance(result, SpawnResult)
    records = [
        record
        for record in caplog.records
        if record.message == "Sandbox startup failed closed for codex: TimeoutError"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert record.exc_info[1] is exception
