"""Synthetic credential operands; no probe reads the operator's real credentials."""

from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks.credential_file_policy import credential_file_access
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import make_agent_definition
from tests.hooks.test_clear_successor_seat import _pane

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        'f=~/.gobby/bootstrap.yaml; test -r "$f"',
        "test -r $'~/.gobby/bootstrap.yaml'",
        r"test -r $'~/.gobby/\x62ootstrap.yaml'",
        "dd if=~/.gobby/local_cli_token of=/dev/null bs=1 count=0",
        'python -c \'import os; open(os.path.expanduser("~/.gobby/")+"bootstrap.yaml")\'',
        "test -r ~/.gobby/Bootstrap.yaml",
        "test -r ~/.Gobby/bootstrap.yaml",
        "test -r ~/.gobby/.SECRET_KEK",
    ],
)
def test_shell_operand_spellings_are_protected(credential_home: Path, command: str) -> None:
    for name in ("bootstrap.yaml", "local_cli_token", ".secret_kek"):
        (credential_home / name).write_text("synthetic")
    assert credential_file_access(
        {"tool_name": "Bash", "cwd": str(credential_home.parent.parent)}, {"command": command}
    )


def test_case_variant_direct_path_is_protected(credential_home: Path) -> None:
    (credential_home / "bootstrap.yaml").write_text("synthetic")
    assert credential_file_access(
        {"tool_name": "Read"}, {"file_path": str(credential_home / "Bootstrap.yaml")}
    )


@pytest.mark.parametrize("name", [".secret_kek", ".secret_salt"])
def test_globbed_parent_directory_is_protected(credential_home: Path, name: str) -> None:
    command = f"cat {credential_home.parent}/.gobb*/{name}"
    assert credential_file_access({"tool_name": "Bash"}, {"command": command})


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("field", ["file_paths", "canonical_file_paths"])
def test_every_normalized_edit_path_is_protected(
    credential_home: Path, tool: str, field: str
) -> None:
    safe = str(credential_home / "logs" / "safe.txt")
    secret = str(credential_home / "bootstrap.yaml")
    assert credential_file_access(
        {"tool_name": tool, field: [safe, secret]}, {"file_path": safe, field: [safe, secret]}
    )


def test_linked_home_and_linked_named_credential_are_protected(
    credential_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / "linked-state"
    alias.symlink_to(credential_home, target_is_directory=True)
    outside = tmp_path / "opaque"
    outside.write_text("synthetic")
    (credential_home / "bootstrap.yaml").symlink_to(outside)
    monkeypatch.setenv("GOBBY_HOME", str(alias))
    assert credential_file_access(
        {"tool_name": "Read"}, {"file_path": str(alias / "bootstrap.yaml")}
    )


@pytest.fixture
def credential_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    root = home / ".gobby"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(root))
    return root


@pytest.mark.parametrize(
    "relative", ["bootstrap.yaml", "local_cli_token", ".secret_kek", "nested/.secret_salt"]
)
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Bash"])
def test_direct_and_shell_credential_operands(
    credential_home: Path, relative: str, tool: str
) -> None:
    target = credential_home / relative
    target.parent.mkdir(exist_ok=True)
    target.write_text("synthetic-secret-never-returned")
    inputs = {
        "Read": {"file_path": str(target)},
        "Grep": {"path": str(target), "pattern": "."},
        "Glob": {"path": str(credential_home), "pattern": relative},
        "Bash": {"command": f"wc -c {target}"},
    }
    assert credential_file_access({"tool_name": tool}, inputs[tool])


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.gobby/bootstrap.yaml",
        'grep token "$HOME/.gobby/local_cli_token"',
        "sed -n 1p ~/.gobby/.secret_kek",
        "head ~/.gobby/nested/.secret_salt",
        "tail ~/.gobby/bootstrap.yaml",
        "awk '{print}' ~/.gobby/bootstrap.yaml",
        "uv run --no-sync python -c 'from pathlib import Path; print(Path(\"~/.gobby/bootstrap.yaml\").expanduser().read_text())'",
        "cp ~/.gobby/local_cli_token /tmp/copied-token",
        "test -r ~/.gobby/bootstrap.yaml",
        "sh -c 'cat ~/.gobby/local_cli_token'",
        "cat ~/.gobby/{bootstrap.yaml,local_cli_token}",
        "cat ~/.gobby/.secret_*",
        "cd ~/.gobby; cat local_cli_token",
        'cat "$GOBBY_HOME/bootstrap.yaml"',
    ],
)
def test_normalized_shell_credential_paths(credential_home: Path, command: str) -> None:
    assert credential_file_access({"tool_name": "Bash"}, {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "gobby status",
        "gobby sessions approve-dirty-paths --reason repair",
        "uv run --no-sync gobby sessions approve-dirty-paths --reason repair",
        "cat ~/.gobby/logs/daemon.log",
        "cat /tmp/bootstrap.yaml",
        "printf '%s' ~/.gobby/logs/daemon.log",
        "printf '%s' bootstrap.yaml",
    ],
)
def test_ordinary_commands_remain_allowed(credential_home: Path, command: str) -> None:
    assert not credential_file_access({"tool_name": "Bash"}, {"command": command})


def test_relative_credential_operand_uses_tool_cwd(credential_home: Path) -> None:
    assert credential_file_access(
        {"tool_name": "Bash", "cwd": str(credential_home)}, {"command": "cat local_cli_token"}
    )


def test_shell_cmd_and_workdir_fields(credential_home: Path) -> None:
    assert credential_file_access(
        {"tool_name": "Bash"}, {"cmd": "cat local_cli_token", "workdir": str(credential_home)}
    )


@pytest.mark.parametrize(
    "relative", ["bootstrap.yaml", "local_cli_token", ".secret_kek", ".secret_salt"]
)
def test_named_credential_symlink_remains_protected(
    credential_home: Path, tmp_path: Path, relative: str
) -> None:
    elsewhere = tmp_path / "opaque-material"
    elsewhere.write_text("synthetic")
    target = credential_home / relative
    target.symlink_to(elsewhere)
    assert credential_file_access({"tool_name": "Read"}, {"file_path": str(target)})


def test_search_from_credential_root_is_protected(credential_home: Path) -> None:
    assert credential_file_access(
        {"tool_name": "Grep", "cwd": str(credential_home)}, {"pattern": "token"}
    )


def test_filtered_glob_and_log_search_remain_allowed(credential_home: Path) -> None:
    assert not credential_file_access(
        {"tool_name": "Glob"}, {"path": str(credential_home), "pattern": "*.log"}
    )
    assert not credential_file_access(
        {"tool_name": "Grep"}, {"path": str(credential_home / "logs"), "pattern": "token"}
    )


@pytest.mark.parametrize("definition", ["fixture-seat", "default"])
@pytest.mark.parametrize("native_child", [False, True])
@pytest.mark.parametrize("tool", ["Read", "Grep", "Bash"])
@pytest.mark.parametrize(
    "relative", ["bootstrap.yaml", "local_cli_token", ".secret_kek", ".secret_salt"]
)
async def test_authenticated_session_policy_covers_native_children(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_home: Path,
    definition: str,
    native_child: bool,
    tool: str,
    relative: str,
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "credential-policy")
    monkeypatch.setenv("GOBBY_HOME", str(credential_home))
    body = make_agent_definition(
        name=definition, prompts={"agent": "Synthetic test seat.", "persona": "Synthetic persona."}
    )
    AgentDefinitionManager(temp_db).create(definition, body.model_dump(mode="json"))
    RuleDefinitionManager(temp_db).create(
        name="fixture-credential-policy",
        definition_json={
            "event": "before_tool",
            "mandatory": True,
            "agent_scope": ["*", "!default"],
            "when": "credential_file_access(event.data, tool_input)",
            "effects": [
                {"type": "block", "reason": "Synthetic policy: credential files are off-limits."}
            ],
        },
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(pane.predecessor_id, {"_agent_type": definition})
    target = str(credential_home / relative)
    inputs: dict[str, dict[str, str]] = {
        "Read": {"file_path": target},
        "Grep": {"path": target, "pattern": "."},
        "Bash": {"command": f"test -r {target}"},
    }
    payload: dict[str, Any] = {
        "session_id": "untrusted-external-id",
        "tool_name": tool,
        "tool_input": inputs[tool],
        "agent_type": "default",
        "variables": {"_agent_type": "default"},
    }
    if native_child:
        payload["agent_id"] = "native-child-42"
    event = ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "_platform_session_id": pane.predecessor_id,
            "input_data": payload,
        }
    )
    if native_child:
        assert event.data["agent_id"] == "native-child-42"
    response = await WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db), session_manager=pane.sessions
    ).evaluate_async(event)
    if definition == "default":
        assert response.decision != "block"
    else:
        assert response.decision == "block"
        assert "Synthetic policy" in (response.reason or "")
    assert "synthetic-secret-never-returned" not in (response.reason or "")
