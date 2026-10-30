"""Replay the provider command and environment a node launch builds and pin them to a corpus.

Plan 2.6 ports this chain to Rust. The corpus at
``crates/gdaemon/tests/fixtures/node_launch/corpus.json`` stores each case's spawn
payload and what the Python oracle produces from it: the provider argv from
``build_cli_command`` (with the claude MCP flags and the codex config overrides the
``prepare_*_spawn`` plans add), and the environment after ``_apply_extra_env``, the
provider layers and ``apply_native_env_policy``. Host paths are replaced with
placeholders, and the Rust replay in ``crates/gdaemon/tests/node_agents.rs`` reads the
same inputs.

Re-record the corpus with ``GOBBY_RECORD_NODE_LAUNCH_CORPUS=1``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from gobby.agents.constants import (
    CLAUDE_MANAGED_AGENT_ENV,
    GOBBY_AGENT_DEPTH,
    GOBBY_AGENT_RUN_ID,
    GOBBY_IS_SPAWNED_AGENT,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_PROJECT_ID,
    GOBBY_PROMPT_FILE,
    GOBBY_SESSION_ID,
    GOBBY_WORKFLOW_NAME,
    MCP_CONNECT_TIMEOUT_MS,
    MCP_CONNECT_TIMEOUT_MS_VALUE,
    MCP_TIMEOUT,
    MCP_TIMEOUT_VALUE,
    get_terminal_env_vars,
    is_spawned_agent,
)
from gobby.agents.credential_inventory import CLI_DENIED_AMBIENT_KEYS
from gobby.agents.spawn_executor_support import _apply_extra_env, _codex_mcp_config_overrides
from gobby.agents.spawners.auth_env import CLI_ENV_ALLOWLIST, UNIVERSAL_ALLOWLIST
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.terminals.native_env_policy import apply_native_env_policy
from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV

if TYPE_CHECKING:
    from gobby.agents.spawn_models import SpawnRequest

_CORPUS = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gdaemon"
    / "tests"
    / "fixtures"
    / "node_launch"
    / "corpus.json"
)
_RECORD_ENV = "GOBBY_RECORD_NODE_LAUNCH_CORPUS"
_PROVIDERS = ("claude", "codex", "grok", "droid", "agy")
_SHELL_SNAPSHOT_ARGS = ["-c", "features.shell_snapshot=false"]
_CLAUDE_SRT_SETTINGS = '{"sandbox": {"enabled": false}}'
# The seven variables every launch carries, plus the workflow when the payload names one.
_IDENTITY_VARIABLES = (
    GOBBY_SESSION_ID,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_AGENT_RUN_ID,
    GOBBY_IS_SPAWNED_AGENT,
    GOBBY_PROJECT_ID,
    GOBBY_AGENT_DEPTH,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_WORKFLOW_NAME,
)
_BLANKED_AMBIENT = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")
_ENDPOINT_VARIABLES = {
    "claude": ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
    "grok": ("GROK_API_BASE", "XAI_API_KEY"),
    "droid": ("FACTORY_API_BASE_URL", "FACTORY_API_KEY"),
}
_ENDPOINT = {"api_base": "https://llm.example.test/v1", "api_token": "sk-launch-corpus-token"}
_EXTRA_ENV = {
    "RUST_LOG": "info",
    "PATH": "/opt/extra/bin:/usr/bin",
    "GOBBY_SESSION_ID": "reserved-session",
    "GOBBY_AGENT_API_TOKEN": "reserved-capability",
    "GOBBY_MACHINE_ID": "reserved-machine",
}
_BARE = {
    "parent_session_id": None,
    "workflow_name": None,
    "model": None,
    "reasoning_effort": None,
}


def _ambient_names() -> list[str]:
    names = set(UNIVERSAL_ALLOWLIST) | set(_BLANKED_AMBIENT)
    for allowed in CLI_ENV_ALLOWLIST.values():
        names |= set(allowed)
    for denied in CLI_DENIED_AMBIENT_KEYS.values():
        names |= set(denied)
    return sorted(names)


def _fixture() -> dict[str, Any]:
    """The inputs every case shares: the node's front door, caches and ambient environment."""
    ambient = {name: f"ambient-{name}" for name in _ambient_names()}
    ambient["PATH"] = "/usr/local/bin:/usr/bin"
    ambient["GOBBY_UNLISTED_AMBIENT"] = "never-forwarded"
    return {
        "front_door_url": "http://127.0.0.1:60999",
        "prompt_file": "<gobby_home>/node/prompts/run-launch-corpus.md",
        "cache_env": {
            "UV_CACHE_DIR": "<gobby_home>/cache/uv",
            "CARGO_HOME": "<gobby_home>/cache/cargo",
            "PATH": "/opt/session/bin:/usr/bin",
        },
        "sandbox_env": {
            "TMPDIR": "<gobby_home>/run/sandbox/run-launch-corpus/tmp",
            "CARGO_TARGET_DIR": "<gobby_home>/run/sandbox/run-launch-corpus/target",
        },
        "ambient": ambient,
    }


def _payload(provider: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "run-launch-corpus",
        "session_id": "session-launch-corpus",
        "parent_session_id": "parent-launch-corpus",
        "project_id": "project-launch-corpus",
        "terminal_id": "terminal-launch-corpus",
        "agent_depth": 2,
        "max_agent_depth": 5,
        "workflow_name": "team-lane",
        "provider": provider,
        "model": "model-x",
        "prompt": "implement the contract",
        "provider_args": [],
        "reasoning_effort": "high",
        "sandbox": {
            "enabled": True,
            "backend": "srt",
            "mode": "restrictive",
            "allow_network": False,
            "extra_read_paths": [],
            "extra_write_paths": [],
            "extra_deny_read_paths": [],
            "extra_deny_write_paths": [],
            "allowed_domains": [],
            "denied_domains": [],
            "allow_git_network": False,
            "allow_package_registries": False,
            "allow_unix_sockets": [],
        },
        "timeout_seconds": 600,
        "title": None,
        "source_root": "/gobby-launch-fixture/source",
        "workspace": {
            "mode": "worktree",
            "path": None,
            "branch_name": "launch-corpus",
            "base_branch": "0.5.0",
        },
        "agent_name": "developer",
        "api_base": None,
        "api_token": None,
        "codex_oss_provider": None,
        "resume_session_id": None,
        "execution_mode": None,
        "code_index_preflight": None,
        "task_id": None,
        "extra_env": {},
        "codex_config_overrides": [],
        "droid_mode": "exec",
        "prewarm_pre_commit_store": False,
        "monitor": {
            "auto_enter_agent_terminals": False,
            "auto_enter_agent_interval_seconds": 30,
            "auto_enter_approval_prompts": False,
            "init_timeout_seconds": 120,
        },
    }
    payload.update(overrides)
    return payload


def _case(name: str, provider: str, mcp_config: bool = False, **overrides: Any) -> dict[str, Any]:
    return {
        "name": name,
        "inputs": {
            "payload": _payload(provider, **overrides),
            "mcp_config": mcp_config,
            "sandbox_args": list(_SHELL_SNAPSHOT_ARGS) if provider == "codex" else [],
        },
    }


def _case_templates() -> list[dict[str, Any]]:
    return [
        _case(
            "claude-fresh",
            "claude",
            mcp_config=True,
            provider_args=["--add-dir", "/opt/shared"],
            extra_env=_EXTRA_ENV,
            **_ENDPOINT,
        ),
        _case("claude-resume", "claude", resume_session_id="claude-resume-id", **_BARE),
        _case(
            "codex-fresh",
            "codex",
            codex_config_overrides=["custom.key=1", 'custom.text="two words"'],
            extra_env=_EXTRA_ENV,
            **_ENDPOINT,
        ),
        _case("codex-resume", "codex", resume_session_id="codex-resume-id", **_BARE),
        _case("codex-oss", "codex", codex_oss_provider="ollama"),
        _case("grok-agent", "grok", extra_env=_EXTRA_ENV, **_ENDPOINT),
        _case("grok-terminal", "grok", execution_mode="interactive"),
        _case("grok-resume", "grok", resume_session_id="grok-resume-id", **_BARE),
        _case("droid-exec", "droid", extra_env=_EXTRA_ENV, **_ENDPOINT),
        _case("droid-exec-resume", "droid", resume_session_id="droid-resume-id", **_BARE),
        _case("droid-interactive", "droid", droid_mode="interactive"),
        _case(
            "droid-interactive-resume",
            "droid",
            droid_mode="interactive",
            resume_session_id="droid-resume-id",
        ),
        _case("agy-fresh", "agy", extra_env=_EXTRA_ENV),
        _case("agy-resume", "agy", resume_session_id="agy-resume-id", **_BARE),
    ]


def _fill(value: Any, layout: dict[str, str]) -> Any:
    if isinstance(value, str):
        for placeholder, path in layout.items():
            value = value.replace(placeholder, path)
        return value
    if isinstance(value, list):
        return [_fill(item, layout) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, layout) for key, item in value.items()}
    return value


def _placeholderize(value: Any, layout: dict[str, str]) -> Any:
    if isinstance(value, str):
        for placeholder, path in layout.items():
            value = value.replace(path, placeholder)
        return value
    if isinstance(value, list):
        return [_placeholderize(item, layout) for item in value]
    if isinstance(value, dict):
        return {key: _placeholderize(item, layout) for key, item in value.items()}
    return value


def _pin_environment(
    monkeypatch: pytest.MonkeyPatch, gobby_home: Path, ambient: dict[str, str]
) -> None:
    for name in _ambient_names():
        monkeypatch.delenv(name, raising=False)
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    # A node installs its managed tools under its own gobby home.
    monkeypatch.setenv(NATIVE_BIN_DIR_ENV, str(gobby_home / "bin"))


def _base_environment(payload: dict[str, Any], fixture: dict[str, Any]) -> dict[str, str]:
    """The variables a node launch starts from, before extra_env and the provider layers."""
    env = {
        "GOBBY_DAEMON_URL": fixture["front_door_url"],
        GOBBY_SESSION_ID: payload["session_id"],
        GOBBY_AGENT_RUN_ID: payload["run_id"],
        GOBBY_IS_SPAWNED_AGENT: (
            "1" if is_spawned_agent(payload["run_id"], payload["agent_depth"]) else "0"
        ),
        GOBBY_PROJECT_ID: payload["project_id"],
        GOBBY_AGENT_DEPTH: str(payload["agent_depth"]),
        GOBBY_MAX_AGENT_DEPTH: str(payload["max_agent_depth"]),
        **fixture["cache_env"],
        GOBBY_PROMPT_FILE: fixture["prompt_file"],
    }
    if payload["parent_session_id"]:
        env[GOBBY_PARENT_SESSION_ID] = payload["parent_session_id"]
    if payload["workflow_name"]:
        env[GOBBY_WORKFLOW_NAME] = payload["workflow_name"]
    return env


def _provider_environment(
    payload: dict[str, Any], fixture: dict[str, Any], strict_mcp: bool
) -> dict[str, str]:
    env = _base_environment(payload, fixture)
    request = cast("SpawnRequest", SimpleNamespace(extra_env=payload["extra_env"]))
    _apply_extra_env(env, request)
    provider = payload["provider"]
    if provider == "claude":
        env.update(CLAUDE_MANAGED_AGENT_ENV)
        env[MCP_TIMEOUT] = MCP_TIMEOUT_VALUE
        if strict_mcp:
            env[MCP_CONNECT_TIMEOUT_MS] = MCP_CONNECT_TIMEOUT_MS_VALUE
    base_name, token_name = _ENDPOINT_VARIABLES.get(provider, (None, None))
    if base_name and payload["api_base"]:
        env[base_name] = payload["api_base"]
    if token_name and payload["api_token"]:
        env[token_name] = payload["api_token"]
    return env


def _claude_command(payload: dict[str, Any], cwd: str, inputs: dict[str, Any]) -> list[str]:
    command, _ = build_cli_command(
        cli="claude",
        prompt=None,
        session_id=payload["session_id"],
        resume_session_id=payload["resume_session_id"],
        auto_approve=True,
        model=payload["model"],
        reasoning_effort=payload["reasoning_effort"],
    )
    if inputs["mcp_config"]:
        command.extend(["--mcp-config", f"{cwd}/.mcp.json", "--strict-mcp-config"])
    command.extend(["--settings", _CLAUDE_SRT_SETTINGS])
    command.extend(inputs["sandbox_args"])
    command.extend(payload["provider_args"])
    if payload["prompt"]:
        command.append(payload["prompt"])
    return command


def _codex_command(
    payload: dict[str, Any],
    cwd: str,
    inputs: dict[str, Any],
    env: dict[str, str],
    sandbox_env: dict[str, str],
) -> list[str]:
    overrides = [
        *_codex_mcp_config_overrides(
            payload["source_root"],
            sandbox_env.get("TMPDIR"),
            managed_identity_env={**env, **sandbox_env},
        ),
        *payload["codex_config_overrides"],
        'sandbox_mode="danger-full-access"',
    ]
    command, _ = build_cli_command(
        cli="codex",
        resume_session_id=payload["resume_session_id"],
        prompt="",
        auto_approve=True,
        working_directory=cwd,
        model=payload["model"],
        codex_oss_provider=payload["codex_oss_provider"],
        reasoning_effort=payload["reasoning_effort"],
        sandbox_args=inputs["sandbox_args"] or None,
        config_overrides=overrides,
        mode="agent",
    )
    return command


def _plain_command(payload: dict[str, Any], cwd: str, inputs: dict[str, Any]) -> list[str]:
    provider = payload["provider"]
    sandbox_args = list(inputs["sandbox_args"])
    mode = "agent"
    if provider == "grok" and payload["execution_mode"] == "interactive":
        mode = "terminal"
    if provider == "droid" and payload["droid_mode"] == "interactive":
        mode = "interactive"
    if provider == "agy":
        # Under an enforced SRT sandbox the agy plan adds --sandbox=false.
        sandbox_args.append("--sandbox=false")
    command, _ = build_cli_command(
        cli=provider,
        mode=mode,
        resume_session_id=payload["resume_session_id"],
        prompt=payload["prompt"],
        auto_approve=True,
        working_directory=cwd,
        model=payload["model"],
        reasoning_effort=payload["reasoning_effort"],
        sandbox_args=sandbox_args or None,
    )
    return command


def _evaluate(
    case: dict[str, Any], fixture: dict[str, Any], root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Run the oracle for one case and return its expectation with host paths replaced."""
    workspace = root / "workspace"
    gobby_home = root / "gobby-home"
    workspace.mkdir(parents=True, exist_ok=True)
    gobby_home.mkdir(parents=True, exist_ok=True)
    layout = {"<workspace>": str(workspace), "<gobby_home>": str(gobby_home)}
    inputs = _fill(case["inputs"], layout)
    filled = _fill(fixture, layout)
    payload = inputs["payload"]
    provider = payload["provider"]
    if inputs["mcp_config"]:
        (workspace / ".mcp.json").write_text("{}\n", encoding="utf-8")
    _pin_environment(monkeypatch, gobby_home, filled["ambient"])
    env = _provider_environment(payload, filled, provider == "claude" and inputs["mcp_config"])
    cwd = str(workspace)
    if provider == "claude":
        command = _claude_command(payload, cwd, inputs)
    elif provider == "codex":
        command = _codex_command(payload, cwd, inputs, env, filled["sandbox_env"])
    else:
        command = _plain_command(payload, cwd, inputs)
    wrapped, policy_env = apply_native_env_policy(
        command, {**env, **filled["sandbox_env"]}, provider
    )
    prefix = wrapped[: len(wrapped) - len(command)]
    expected = {"command": command, "argv_prefix": prefix, "env": policy_env}
    return cast("dict[str, Any]", _placeholderize(expected, layout))


def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture()
    cases = []
    for template in _case_templates():
        root = tmp_path / "record" / template["name"]
        cases.append({**template, "expected": _evaluate(template, fixture, root, monkeypatch)})
    corpus = {"version": 1, "recorded_on": sys.platform, "fixture": fixture, "cases": cases}
    _CORPUS.parent.mkdir(parents=True, exist_ok=True)
    _CORPUS.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_corpus() -> dict[str, Any]:
    if not _CORPUS.is_file():
        pytest.fail(
            f"node launch corpus is missing at {_CORPUS}; "
            f"record it with {_RECORD_ENV}=1 before the Python and Rust replays run"
        )
    corpus: dict[str, Any] = json.loads(_CORPUS.read_text(encoding="utf-8"))
    if not corpus.get("cases"):
        pytest.fail(f"node launch corpus at {_CORPUS} has no cases; re-record it")
    return corpus


def test_corpus_matches_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get(_RECORD_ENV) == "1":
        _record(tmp_path, monkeypatch)
    corpus = _load_corpus()

    mismatches: list[str] = []
    if corpus["fixture"] != _fixture():
        mismatches.append("the shared fixture differs from the recorded one")
    covered = {case["inputs"]["payload"]["provider"] for case in corpus["cases"]}
    if covered != set(_PROVIDERS):
        mismatches.append(f"corpus covers {sorted(covered)}, not every provider")
    for case in corpus["cases"]:
        root = tmp_path / "check" / case["name"]
        expected = _evaluate(case, corpus["fixture"], root, monkeypatch)
        if expected != case["expected"]:
            changed = sorted(key for key in expected if expected[key] != case["expected"].get(key))
            mismatches.append(f"{case['name']}: differs at {changed}")
    assert not mismatches, "\n".join(mismatches)


def test_identity_environment_matches_terminal_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity variables the corpus builds are the ones get_terminal_env_vars sets."""
    fixture = _fixture()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _pin_environment(monkeypatch, tmp_path / "gobby-home", fixture["ambient"])
    for template in _case_templates():
        payload = template["inputs"]["payload"]
        oracle = get_terminal_env_vars(
            session_id=payload["session_id"],
            parent_session_id=payload["parent_session_id"] or "",
            agent_run_id=payload["run_id"],
            project_id=payload["project_id"],
            checkout_root=workspace,
            workflow_name=payload["workflow_name"],
            agent_depth=payload["agent_depth"],
            max_agent_depth=payload["max_agent_depth"],
            prompt_file=fixture["prompt_file"],
        )
        ours = _base_environment(payload, fixture)
        for name in (*_IDENTITY_VARIABLES, GOBBY_PROMPT_FILE):
            assert ours.get(name) == oracle.get(name), f"{template['name']}: {name}"
