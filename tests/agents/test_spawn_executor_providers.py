import json
import logging
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import spawn_executor_codex
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import (
    ProviderSpawnPlan,
    _prepare_claude_reviewer_state,
    _prepare_provider_sandbox,
    prepare_claude_spawn,
    prepare_codex_spawn,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError, render_srt_settings
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
    assert ("CLAUDE_CONFIG_DIR" in plan.env) is quiet
    assert ("XDG_STATE_HOME" in plan.env) is quiet


@pytest.mark.parametrize("configured_home", [False, True])
@pytest.mark.parametrize("secure_storage", [None, "", "/explicit-auth-home"])
def test_claude_reviewer_state_preserves_auth_and_hooks_without_copying_host_state(
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

    _prepare_claude_reviewer_state(request, env)

    expected_auth = secure_storage
    if expected_auth is None:
        expected_auth = str(host_config) if configured_home else ""
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == expected_auth
    config = Path(env["CLAUDE_CONFIG_DIR"])
    assert config == cache / "claude"
    assert Path(env["XDG_STATE_HOME"]).is_relative_to(cache)
    assert (config / "settings.json").resolve() == settings
    assert (config / "settings.json").read_text(encoding="utf-8") == settings.read_text(
        encoding="utf-8"
    )
    assert json.loads((config / ".claude.json").read_text(encoding="utf-8")) == {
        "hasCompletedOnboarding": True,
        "bypassPermissionsModeAccepted": True,
    }
    assert (config / "projects" / "-workspace").is_dir()
    assert (config / ".claude.json").stat().st_mode & 0o777 == 0o600
    assert not (config / ".credentials.json").exists()
    assert credentials.read_text(encoding="utf-8") == "host-only-credential-sentinel"
    assert host_global.read_text(encoding="utf-8") == '{"host_only": true}'


def test_claude_reviewer_state_refuses_redirected_config_before_writing(
    tmp_path: Path,
) -> None:
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
        _prepare_claude_reviewer_state(request, env)

    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert "CLAUDE_CONFIG_DIR" not in env


def test_claude_reviewer_state_does_not_seed_unapproved_workspace_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "host-home")
    request, _, _ = _sandbox_request()
    request = replace(request, auto_approve=False)
    env = {"XDG_CACHE_HOME": str(tmp_path / "run-cache")}

    _prepare_claude_reviewer_state(request, env)

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
