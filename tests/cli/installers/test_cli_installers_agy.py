"""Tests for the AGY CLI installer."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.adapters.agy_contract import (
    AGY_FLAT_HOOK_NAMES,
    AGY_GOBBY_HOOK_NAME,
    AGY_GROUPED_HOOK_NAMES,
    AGY_HOOK_NAMES,
)
from gobby.cli.installers.agy import install_agy, uninstall_agy

pytestmark = pytest.mark.unit


@pytest.fixture
def project_path(temp_dir: Path) -> Path:
    project = temp_dir / "project"
    project.mkdir()
    return project


@pytest.fixture
def agy_env(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.delenv("GOBBY_HOOKS_DIR", raising=False)
    monkeypatch.delenv("GOBBY_AGY_HOOKS_FILE", raising=False)
    monkeypatch.delenv("GOBBY_AGY_MCP_FILE", raising=False)
    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
        patch("gobby.cli.installers.agy.install_shared_content", return_value={"plugins": []}),
        patch("gobby.cli.installers.agy.install_cli_content", return_value={"commands": []}),
        patch("gobby.cli.installers.agy.shutil.which", return_value=None),
    ):
        yield temp_dir


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    assert isinstance(payload, dict)
    return payload


def _handler_timeouts(settings: dict[str, Any]) -> list[int]:
    gobby_hook = settings[AGY_GOBBY_HOOK_NAME]
    timeouts: list[int] = []
    for hook_type in AGY_FLAT_HOOK_NAMES:
        timeout = gobby_hook[hook_type][0]["timeout"]
        assert isinstance(timeout, int)
        timeouts.append(timeout)
    for hook_type in AGY_GROUPED_HOOK_NAMES:
        timeout = gobby_hook[hook_type][0]["hooks"][0]["timeout"]
        assert isinstance(timeout, int)
        timeouts.append(timeout)
    return timeouts


def _agy_hooks_listing(*, error: str | None = None, include_gobby: bool = True) -> dict[str, Any]:
    if error is not None:
        return {
            "conversation_id": "",
            "status": "ERROR",
            "response": "",
            "error": error,
            "command": {"name": "hooks", "data": {"hooks": []}},
        }
    hooks: list[dict[str, Any]] = []
    if include_gobby:
        hooks.append(
            {
                "name": AGY_GOBBY_HOOK_NAME,
                "enabled": True,
                "source": "~/.gemini/config/hooks.json",
                "actions": [
                    {
                        "event": hook_type,
                        "type": "command",
                        "command": f"ghook --gobby-owned --cli=agy --type={hook_type}",
                    }
                    for hook_type in AGY_HOOK_NAMES
                ],
            }
        )
    return {
        "conversation_id": "",
        "status": "SUCCESS",
        "response": "",
        "command": {"name": "hooks", "data": {"hooks": hooks}},
    }


def _fake_agy_hooks_run(
    payload: dict[str, Any], *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["/usr/bin/agy", "-p", "/hooks", "--output-format", "json"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


@contextmanager
def _agy_probe(*, which: str | None, run: Any = None) -> Iterator[Any]:
    with ExitStack() as stack:
        stack.enter_context(patch("gobby.cli.installers.agy.shutil.which", return_value=which))
        mocked_run = None
        if run is not None:
            mocked_run = stack.enter_context(
                patch("gobby.cli.installers.agy.subprocess.run", return_value=run)
            )
        yield mocked_run


def test_install_agy_global_writes_vendor_hooks_and_mcp(
    project_path: Path,
    agy_env: Path,
) -> None:
    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    assert tuple(result["hooks_installed"]) == AGY_HOOK_NAMES
    assert result["trust"]["skipped"] is True
    assert result["trust"]["files_written"] == []
    assert not (agy_env / ".antigravitycli").exists()

    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    settings = _load_json(hooks_file)
    assert list(settings) == [AGY_GOBBY_HOOK_NAME]

    gobby_hook = settings[AGY_GOBBY_HOOK_NAME]
    assert set(gobby_hook) == set(AGY_HOOK_NAMES)
    for hook_type in AGY_FLAT_HOOK_NAMES:
        command = gobby_hook[hook_type][0]["command"]
        expected = f"/Users/test/.gobby/bin/ghook --gobby-owned --cli=agy --type={hook_type}"
        assert command == expected
    for hook_type in AGY_GROUPED_HOOK_NAMES:
        command = gobby_hook[hook_type][0]["hooks"][0]["command"]
        expected = f"/Users/test/.gobby/bin/ghook --gobby-owned --cli=agy --type={hook_type}"
        assert command == expected

    mcp = _load_json(agy_env / ".gemini" / "config" / "mcp_config.json")
    assert mcp["mcpServers"]["gobby"]["type"] == "stdio"
    assert mcp["mcpServers"]["gobby"]["args"] == ["mcp-server"]


def test_install_agy_rejects_project_mode(
    project_path: Path,
    agy_env: Path,
) -> None:
    result = install_agy(project_path, mode="project")

    assert result["success"] is False
    assert result["error"] == "AGY integration only supports global install mode"
    assert not (agy_env / ".gemini" / "config" / "hooks.json").exists()
    assert not (agy_env / ".gemini" / "config" / "mcp_config.json").exists()
    assert not (project_path / ".gemini" / "config" / "hooks.json").exists()


def test_install_agy_preserves_existing_mcp_servers(
    project_path: Path,
    agy_env: Path,
) -> None:
    mcp_file = agy_env / ".gemini" / "config" / "mcp_config.json"
    mcp_file.parent.mkdir(parents=True)
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"command": "node"},
                    "beta": {"command": "python"},
                }
            }
        )
    )

    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    mcp_servers = _load_json(mcp_file)["mcpServers"]
    assert set(mcp_servers) == {"alpha", "beta", "gobby"}
    assert mcp_servers["gobby"]["type"] == "stdio"


def test_install_agy_preserves_third_party_named_hooks(
    project_path: Path,
    agy_env: Path,
) -> None:
    """Gobby owns one named hook; a neighbour's entry survives byte-identical."""
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    lint_checker = {
        "PostToolUse": [
            {
                "matcher": "run_command",
                "hooks": [{"type": "command", "command": "./scripts/lint.sh", "timeout": 10}],
            }
        ]
    }
    safety_gate = {
        "enabled": False,
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "./safety.sh"}]}],
    }
    hooks_file.write_text(json.dumps({"lint-checker": lint_checker, "safety-gate": safety_gate}))

    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    settings = _load_json(hooks_file)
    assert settings["lint-checker"] == lint_checker
    assert settings["safety-gate"] == safety_gate
    gobby_hook = settings[AGY_GOBBY_HOOK_NAME]
    assert gobby_hook["PreToolUse"][0]["hooks"][0]["command"].endswith(
        "--cli=agy --type=PreToolUse"
    )


def test_install_agy_replaces_legacy_literal_hooks_key(
    project_path: Path,
    agy_env: Path,
) -> None:
    """The pre-fix layout wrote a literal "hooks" key AGY rejected wholesale."""
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreInvocation": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "/Users/test/.gobby/bin/ghook --gobby-owned "
                                        "--cli=agy --type=PreInvocation"
                                    ),
                                }
                            ]
                        }
                    ],
                }
            }
        )
    )

    result = install_agy(project_path, mode="global")

    assert result["success"] is True
    settings = _load_json(hooks_file)
    assert "hooks" not in settings
    assert list(settings) == [AGY_GOBBY_HOOK_NAME]


def test_install_agy_is_idempotent(project_path: Path, agy_env: Path) -> None:
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"

    assert install_agy(project_path, mode="global")["success"] is True
    first = hooks_file.read_text()
    assert install_agy(project_path, mode="global")["success"] is True

    assert hooks_file.read_text() == first


def test_uninstall_agy_removes_only_gobby_entries(
    project_path: Path,
    agy_env: Path,
) -> None:
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    lint_checker = {
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "custom"}]}]
    }
    hooks_file.write_text(
        json.dumps(
            {
                "lint-checker": lint_checker,
                AGY_GOBBY_HOOK_NAME: {
                    "PreInvocation": [
                        {
                            "type": "command",
                            "command": "ghook --gobby-owned --cli=agy --type=PreInvocation",
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": ("ghook --gobby-owned --cli=agy --type=PreToolUse"),
                                }
                            ],
                        }
                    ],
                },
            }
        )
    )
    mcp_file = agy_env / ".gemini" / "config" / "mcp_config.json"
    mcp_file.write_text(
        json.dumps({"mcpServers": {"gobby": {"command": "gobby"}, "other": {"command": "node"}}})
    )

    result = uninstall_agy(project_path, mode="global")

    assert result["success"] is True
    assert result["hooks_removed"] == ["PreInvocation", "PreToolUse"]
    settings = _load_json(hooks_file)
    assert AGY_GOBBY_HOOK_NAME not in settings
    assert settings["lint-checker"] == lint_checker
    assert set(_load_json(mcp_file)["mcpServers"]) == {"other"}


def test_install_agy_applies_hook_timeout_seconds_to_both_layouts(
    project_path: Path,
    agy_env: Path,
) -> None:
    result = install_agy(project_path, mode="global", hook_timeout_seconds=150)

    assert result["success"] is True
    timeouts = _handler_timeouts(_load_json(agy_env / ".gemini" / "config" / "hooks.json"))
    assert timeouts == [150] * len(AGY_HOOK_NAMES)


def test_install_agy_rejects_non_positive_hook_timeout(
    project_path: Path,
    agy_env: Path,
) -> None:
    result = install_agy(project_path, mode="global", hook_timeout_seconds=0)

    assert result["success"] is False
    assert result["error"] == "hook_timeout_seconds must be positive"
    assert not (agy_env / ".gemini" / "config" / "hooks.json").exists()


def test_install_agy_skips_verification_when_agy_missing(
    project_path: Path,
    agy_env: Path,
) -> None:
    with _agy_probe(which=None):
        result = install_agy(project_path, mode="global")

    assert result["success"] is True
    assert result.get("verification") == "skipped"
    assert "verified" not in result


def test_install_agy_verifies_registration_when_agy_lists_gobby(
    project_path: Path,
    agy_env: Path,
) -> None:
    completed = _fake_agy_hooks_run(_agy_hooks_listing())
    with _agy_probe(which="/usr/bin/agy", run=completed):
        result = install_agy(project_path, mode="global")

    assert result["success"] is True
    assert result.get("verified") is True
    assert result.get("verified_hooks") == list(AGY_HOOK_NAMES)


def test_install_agy_reports_unverified_when_agy_rejects_hooks(
    project_path: Path,
    agy_env: Path,
) -> None:
    error = "invalid hook \"hooks\": command hook must specify 'command'"
    completed = _fake_agy_hooks_run(_agy_hooks_listing(error=error), returncode=1)
    with _agy_probe(which="/usr/bin/agy", run=completed):
        result = install_agy(project_path, mode="global")

    assert result["success"] is True
    assert result.get("verified") is False
    assert result.get("verification_error") == error


def test_install_agy_is_idempotent_and_still_verifies(
    project_path: Path,
    agy_env: Path,
) -> None:
    hooks_file = agy_env / ".gemini" / "config" / "hooks.json"
    completed = _fake_agy_hooks_run(_agy_hooks_listing())
    with _agy_probe(which="/usr/bin/agy", run=completed) as run_hooks:
        first = install_agy(project_path, mode="global")
        written = hooks_file.read_text()
        second = install_agy(project_path, mode="global")

    assert first["success"] is True
    assert second["success"] is True
    assert first.get("verified") is True
    assert second.get("verified") is True
    assert hooks_file.read_text() == written
    assert run_hooks.call_count == 2
    assert run_hooks.call_args.args[0] == [
        "/usr/bin/agy",
        "-p",
        "/hooks",
        "--output-format",
        "json",
    ]
