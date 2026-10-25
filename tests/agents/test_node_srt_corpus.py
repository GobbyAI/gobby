"""Replay the spawn-time SRT policy chain and pin it to a committed corpus.

Plan 2.4 ports this chain to Rust. The corpus at
``crates/gdaemon/tests/fixtures/node_srt/corpus.json`` stores each case's inputs
and the output the Python oracle produces: the canonical settings JSON and the
environment additions. Host paths are replaced with placeholders, and the Rust
replay in ``crates/gdaemon/tests/node_agents.rs`` reads the same inputs.

Re-record the corpus with ``GOBBY_RECORD_NODE_SRT_CORPUS=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.cargo_target import (
    ensure_agent_cargo_target_dir,
    sandbox_checkout_cargo_target_dir,
)
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.sandbox_policy import gcode_runtime_write_exceptions, prepare_sandbox_run_paths
from gobby.agents.spawn_cache_policy import sandbox_config_for_spawn
from gobby.agents.srt_settings import render_srt_settings
from gobby.utils.project_context import get_project_sandbox_config

_CORPUS = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gdaemon"
    / "tests"
    / "fixtures"
    / "node_srt"
    / "corpus.json"
)
_RECORD_ENV = "GOBBY_RECORD_NODE_SRT_CORPUS"
_FIXTURE_CHECKOUT = "/gobby-srt-fixture/checkout"
_RUN_ID = "node-srt-corpus"
_PROJECT_ID = "node-srt-project"
_SESSION_ID = "node-srt-session"
_DAEMON_PORT = 60887
_WEBSOCKET_PORT = 60888
_PROVIDERS = ("claude", "codex", "grok", "droid", "agy")
# Variables the oracle reads from the process environment. Each case pins them
# explicitly, and any that the case does not set is removed.
_HOST_ENVIRONMENT = (
    "CARGO_HOME",
    "CARGO_TARGET_DIR",
    "UV_CACHE_DIR",
    "PRE_COMMIT_HOME",
    "RTK_DB_PATH",
    "RTK_TEE_DIR",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    "GOBBY_PROMPT_FILE",
    "APPDATA",
    "LOCALAPPDATA",
)


def _sandbox(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": True,
        "backend": "srt",
        "mode": "permissive",
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
    }
    payload.update(overrides)
    return payload


def _environment() -> dict[str, str]:
    return {
        "HOME": "{home}",
        "GOBBY_HOME": "{gobby_home}",
        "GOBBY_SESSION_ID": _SESSION_ID,
        "PATH": "{bin}:/usr/bin:/bin",
        "XDG_CACHE_HOME": "{home}/.cache",
        "XDG_CONFIG_HOME": "{home}/.config",
        "XDG_DATA_HOME": "{home}/.local/share",
        # The spawn caches live under the temp dir, which Python reads from TMPDIR.
        "TMPDIR": "{tmp}",
    }


def _case(
    name: str,
    provider: str,
    *,
    sandbox: dict[str, Any] | None = None,
    source_root: str = "{workspace}",
    home_tree: tuple[str, ...] = (),
    project_json: dict[str, Any] | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": platform,
        "inputs": {
            "provider": provider,
            "sandbox": sandbox if sandbox is not None else _sandbox(),
            "source_root": source_root,
            "project_id": _PROJECT_ID,
            "session_id": _SESSION_ID,
            "workspace": "{workspace}",
            "run_id": _RUN_ID,
            "daemon_port": _DAEMON_PORT,
            "websocket_port": _WEBSOCKET_PORT,
            "provider_executable": "{bin}/" + provider,
            "prewarm_pre_commit_store": False,
            "home_tree": list(home_tree),
            "project_json": project_json,
            "env": _environment(),
        },
    }


def _case_templates() -> list[dict[str, Any]]:
    cases = [_case(f"{provider}-srt-empty", provider) for provider in _PROVIDERS]
    cases.append(
        _case(
            "codex-domains-git-registries",
            "codex",
            sandbox=_sandbox(
                allowed_domains=["crates.io", "example.com"],
                allow_git_network=True,
                allow_package_registries=True,
            ),
        )
    )
    cases.append(
        _case(
            "claude-extra-write-paths",
            "claude",
            sandbox=_sandbox(extra_write_paths=["{home}/scratch/extra"]),
            home_tree=("scratch",),
        )
    )
    cases.append(
        _case(
            "claude-nextest-lane",
            "claude",
            source_root="{main}",
            project_json={"sandbox": {"extra_write_paths": [".nextest"]}},
        )
    )
    cases.append(
        _case(
            "claude-rtk-state",
            "claude",
            home_tree=("Library/Application Support/rtk",),
            platform="darwin",
        )
    )
    cases.append(
        _case(
            "codex-unicode-write-paths",
            "codex",
            sandbox=_sandbox(extra_write_paths=["{home}/café/数据"]),
            home_tree=("café/数据",),
        )
    )
    cases.append(
        _case(
            "claude-non-bmp-write-paths",
            "claude",
            sandbox=_sandbox(extra_write_paths=["{home}/🙂/extra"]),
            home_tree=("🙂/extra",),
        )
    )
    return cases


def _refusal_templates() -> list[tuple[str, dict[str, Any], str]]:
    return [
        (
            "sensitive-grant",
            _case(
                "sensitive-grant",
                "claude",
                sandbox=_sandbox(extra_write_paths=["{gobby_home}/grants/extra"]),
            ),
            "sandbox allow path contains sensitive root: ",
        ),
        (
            "credential-grant",
            _case(
                "credential-grant",
                "claude",
                sandbox=_sandbox(extra_write_paths=["{home}/.ssh/extra"]),
            ),
            "sandbox read grant shadows credential deny: ",
        ),
    ]


def _layout(case_dir: Path) -> dict[str, str]:
    root = case_dir.resolve()
    layout = {
        "home": root / "home",
        "gobby_home": root / "gobby-home",
        "workspace": root / "workspace",
        "main": root / "main",
        "bin": root / "bin",
        "tmp": root / "tmp",
    }
    named = {name: str(path) for name, path in layout.items()}
    named["run_root"] = str(root / "gobby-home" / "run" / "sandbox" / _RUN_ID)
    # The SRT socket root is fixed under the system temp dir and keyed by uid.
    named["srt_socket"] = f"{os.path.realpath('/tmp')}/gobby-srt-{os.getuid()}"
    # The oracle also emits the unresolved spelling of the same root.
    named["srt_socket_literal"] = f"/tmp/gobby-srt-{os.getuid()}"
    return named


def _pin_checkout(layout: dict[str, str]) -> None:
    """Add the workspace-keyed directories, whose names hash the workspace path."""
    workspace = Path(layout["workspace"])
    layout["checkout"] = sandbox_checkout_cargo_target_dir(workspace).name
    layout["gcode_runtime"] = gcode_runtime_write_exceptions(workspace)[0]


def _digest_table(case_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Pin the path-hash algorithms on a fixed path, so Rust checks them directly."""
    layout = _layout(case_dir)
    _pin_environment(monkeypatch, _fill(_environment(), layout))
    fixture = Path(_FIXTURE_CHECKOUT)
    return {
        "fixture_checkout": _FIXTURE_CHECKOUT,
        "checkout_component": sandbox_checkout_cargo_target_dir(fixture).name,
        "gcode_runtime_digest": Path(gcode_runtime_write_exceptions(fixture)[0]).name,
    }


def _fill(value: Any, layout: dict[str, str]) -> Any:
    if isinstance(value, str):
        for name, real in layout.items():
            value = value.replace("{" + name + "}", real)
        return value
    if isinstance(value, list):
        return [_fill(item, layout) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, layout) for key, item in value.items()}
    return value


def _placeholderize(text: str, layout: dict[str, str]) -> str:
    for name, real in sorted(layout.items(), key=lambda item: len(item[1]), reverse=True):
        text = text.replace(real, "{" + name + "}")
    return text


def _materialize(inputs: dict[str, Any], layout: dict[str, str]) -> None:
    for name in ("home", "gobby_home", "bin", "tmp", "workspace", "main"):
        Path(layout[name]).mkdir(parents=True, exist_ok=True)
    for relative in inputs["home_tree"]:
        (Path(layout["home"]) / relative).mkdir(parents=True, exist_ok=True)
    for provider in _PROVIDERS:
        executable = Path(layout["bin"]) / provider
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    for repo in (Path(layout["workspace"]), Path(layout["main"])):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if inputs["project_json"] is not None:
        config_dir = Path(layout["main"]) / ".gobby"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "project.json").write_text(
            json.dumps(inputs["project_json"]), encoding="utf-8"
        )


def _pin_environment(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name in _HOST_ENVIRONMENT:
        if name not in env:
            monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _run_chain(inputs: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Replay the spawn-time chain the oracle runs for one srt launch."""
    provider = str(inputs["provider"])
    workspace = Path(inputs["workspace"])
    main = Path(inputs["source_root"])
    base_env: dict[str, str] = dict(inputs["env"])
    env = dict(base_env)

    config = SandboxConfig.model_validate(inputs["sandbox"])
    nextest_store = str(main.resolve() / ".nextest")
    main_config = get_project_sandbox_config(main)
    if (
        main_config is not None
        and nextest_store in main_config.extra_write_paths
        and nextest_store not in config.extra_write_paths
    ):
        config = config.model_copy(
            update={"extra_write_paths": [*config.extra_write_paths, nextest_store]}
        )

    env["CARGO_TARGET_DIR"] = ensure_agent_cargo_target_dir(workspace, str(inputs["project_id"]))
    spawned = sandbox_config_for_spawn(
        config, env, agent_name=None, project_path=str(main), resume_metadata_json=None
    )
    assert spawned is not None

    run_paths = prepare_sandbox_run_paths(
        str(inputs["run_id"]),
        env,
        workspace=workspace,
        short_tmp=False,
        prewarm_pre_commit_store=False,
    )
    run_environment = run_paths.environment(provider)
    effective = spawned.model_copy(
        update={
            "extra_write_paths": [
                *spawned.extra_write_paths,
                *(str(path) for path in run_paths.writable),
                str(run_paths.cargo_home),
                str(run_paths.cargo_target),
            ]
        }
    )
    effective_env = {**env, **run_environment}
    paths = asyncio.run(
        compute_sandbox_paths(
            effective,
            str(workspace),
            int(inputs["daemon_port"]),
            gobby_websocket_port=int(inputs["websocket_port"]),
            provider=provider,
            provider_executable=str(inputs["provider_executable"]),
            api_base=None,
            env=effective_env,
        )
    )
    settings = json.dumps(render_srt_settings(paths), sort_keys=True, separators=(",", ":"))
    additions = {key: value for key, value in effective_env.items() if base_env.get(key) != value}
    return settings, additions


def _evaluate(
    template: dict[str, Any], case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, str], dict[str, str]]:
    layout = _layout(case_dir)
    inputs = _fill(template["inputs"], layout)
    _materialize(inputs, layout)
    _pin_environment(monkeypatch, inputs["env"])
    monkeypatch.setattr(tempfile, "tempdir", layout["tmp"])
    _pin_checkout(layout)
    settings, env = _run_chain(inputs)
    return settings, env, layout


def _refusal(
    template: dict[str, Any], case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, str]]:
    layout = _layout(case_dir)
    inputs = _fill(template["inputs"], layout)
    _materialize(inputs, layout)
    _pin_environment(monkeypatch, inputs["env"])
    monkeypatch.setattr(tempfile, "tempdir", layout["tmp"])
    _pin_checkout(layout)
    try:
        _run_chain(inputs)
    except ValueError as exc:
        return str(exc), layout
    pytest.fail(f"{template['name']}: the oracle accepted a grant it must refuse")


def _encode_settings(settings: str, table: list[str]) -> dict[str, Any]:
    """Parse one case's settings, moving denyWrite into the shared table as indices.

    The sensitive deny list repeats for every case with a few entries changed, so
    one copy of each distinct entry keeps the corpus small.
    """
    document: dict[str, Any] = json.loads(settings)
    positions = {entry: index for index, entry in enumerate(table)}
    indices: list[int] = []
    for entry in document["filesystem"]["denyWrite"]:
        if entry not in positions:
            positions[entry] = len(table)
            table.append(entry)
        indices.append(positions[entry])
    document["filesystem"]["denyWrite"] = indices
    return document


def _decode_settings(document: dict[str, Any], table: list[str]) -> str:
    """Rebuild the oracle's exact settings text from an encoded case.

    The oracle's deny lists pass through deny_paths, which keeps the first of each
    repeated path. On Linux the two SRT socket aliases are one path, so the filled
    corpus repeats it. Collapsing repeats here matches the oracle on every host.
    """
    indices: list[int] = document["filesystem"]["denyWrite"]
    filesystem = document["filesystem"]
    filesystem["denyWrite"] = list(dict.fromkeys(table[index] for index in indices))
    filesystem["denyRead"] = list(dict.fromkeys(filesystem["denyRead"]))
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases: list[dict[str, Any]] = []
    table: list[str] = []
    for template in _case_templates():
        settings, env, layout = _evaluate(
            template, tmp_path / "record" / template["name"], monkeypatch
        )
        cases.append(
            {
                "name": template["name"],
                "platform": template["platform"],
                "inputs": template["inputs"],
                "expected": {
                    "settings": _encode_settings(_placeholderize(settings, layout), table),
                    "env": {key: _placeholderize(value, layout) for key, value in env.items()},
                },
            }
        )
    refusals: list[dict[str, Any]] = []
    for name, template, prefix in _refusal_templates():
        message, layout = _refusal(template, tmp_path / "record" / name, monkeypatch)
        assert message.startswith(prefix), message
        refusals.append(
            {"name": name, "inputs": template["inputs"], "error": _placeholderize(message, layout)}
        )
    corpus = {
        "version": 1,
        "recorded_on": sys.platform,
        "path_digests": _digest_table(tmp_path / "record" / "fixture", monkeypatch),
        "deny_write_table": table,
        "cases": cases,
        "refusals": refusals,
    }
    _CORPUS.parent.mkdir(parents=True, exist_ok=True)
    _CORPUS.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_corpus() -> dict[str, Any]:
    if not _CORPUS.is_file():
        pytest.fail(
            f"node SRT corpus is missing at {_CORPUS}; "
            f"record it with {_RECORD_ENV}=1 before the Python and Rust replays run"
        )
    corpus: dict[str, Any] = json.loads(_CORPUS.read_text(encoding="utf-8"))
    if not corpus.get("cases"):
        pytest.fail(f"node SRT corpus at {_CORPUS} has no cases; re-record it")
    return corpus


def test_corpus_matches_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get(_RECORD_ENV) == "1":
        _record(tmp_path, monkeypatch)
    corpus = _load_corpus()

    mismatches: list[str] = []
    digests = _digest_table(tmp_path / "check" / "fixture", monkeypatch)
    if corpus.get("path_digests") != digests:
        mismatches.append(f"path digests differ: {digests}")
    for case in corpus["cases"]:
        if case["platform"] not in (None, sys.platform):
            continue
        settings, env, layout = _evaluate(case, tmp_path / "check" / case["name"], monkeypatch)
        # Compare filled-in paths. Placeholder text would differ by host, since the
        # two SRT socket spellings are equal wherever /tmp is not a symlink.
        table = _fill(corpus["deny_write_table"], layout)
        expected_settings = _decode_settings(_fill(case["expected"]["settings"], layout), table)
        expected_env = _fill(case["expected"]["env"], layout)
        if settings != expected_settings:
            start = next(
                (
                    index
                    for index, (left, right) in enumerate(
                        zip(settings, expected_settings, strict=False)
                    )
                    if left != right
                ),
                min(len(settings), len(expected_settings)),
            )
            mismatches.append(
                f"{case['name']}: settings differ at {start}: "
                f"actual {settings[start : start + 160]!r} "
                f"expected {expected_settings[start : start + 160]!r}"
            )
        if env != expected_env:
            changed = sorted(
                key for key in {*env, *expected_env} if env.get(key) != expected_env.get(key)
            )
            mismatches.append(f"{case['name']}: environment differs at {changed}")

    for refusal in corpus["refusals"]:
        message, layout = _refusal(
            {"name": refusal["name"], "inputs": refusal["inputs"]},
            tmp_path / "check" / refusal["name"],
            monkeypatch,
        )
        if message != _fill(refusal["error"], layout):
            mismatches.append(f"{refusal['name']}: refusal message {message!r}")

    assert not mismatches, "\n".join(mismatches)
