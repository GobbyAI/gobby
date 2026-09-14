"""Replay gcode outages through every provider's real hook translation (#22345)."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.base import BaseAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.code_navigation_recovery import navigation_requires_index
from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit

Native = Callable[[Path], dict[str, Any]]

OUTAGE = json.dumps({"error": "schema_mismatch", "message": "schema identity mismatch"})
FAILURE = f"Exit code 2\n{OUTAGE}"
OUTLINE = "gcode outline src/app.py"
PIPED = f"{OUTLINE} 2>&1 | head -40"
BATCH = f'gcode grep -F "VALUE" src -m 30; echo "---"; {OUTLINE}'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def translate(adapter: BaseAdapter, native: dict[str, Any], repo: Path) -> dict[str, Any]:
    """Mirror the rule engine: adapter data plus session cwd and project, renormalized."""
    event = adapter.translate_to_hook_event(native)
    assert event is not None
    event.data.setdefault("cwd", event.cwd or str(repo))
    event.data.setdefault("project_path", str(repo))
    return normalize_tool_fields(event.data)


def read(repo: Path, path: str) -> dict[str, Any]:
    return normalize_tool_fields(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(repo / path)},
            "cwd": str(repo),
            "project_path": str(repo),
        }
    )


def shell_hook(
    hook_type: str, tool: str, command: str, repo: Path, **fields: Any
) -> dict[str, Any]:
    return {
        "hook_type": hook_type,
        "input_data": {
            "session_id": "replay",
            "cwd": str(repo),
            "tool_use_id": "t1",
            "tool_name": tool,
            "tool_input": {"command": command},
            **fields,
        },
    }


def codex_item(command: str, output: str, repo: Path) -> dict[str, Any]:
    item = {
        "type": "commandExecution",
        "id": "i1",
        "command": f"/bin/zsh -lc {shlex.quote(command)}",
        "cwd": str(repo),
        "aggregatedOutput": output,
        "exitCode": 2,
        "status": "failed",
    }
    return {"method": "item/completed", "params": {"threadId": "replay", "item": item}}


def grok_hook(command: str, text: str, repo: Path) -> dict[str, Any]:
    return {
        "hook_type": "post_tool_use",
        "input_data": {
            "hookEventName": "post_tool_use",
            "sessionId": "replay",
            "cwd": str(repo),
            "toolName": "run_terminal_command",
            "toolUseId": "t1",
            "toolInput": {"command": command},
            "toolResult": {
                "type": "Bash",
                "output": list(text.split("\n", 1)[1].encode()),
                "output_for_prompt": text,
                "exit_code": 2,
                "command": command,
                "current_dir": str(repo),
            },
        },
    }


def agy_hook(hook_type: str, command: str, repo: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversationId": "replay",
        "stepIdx": 3,
        "toolCall": {"name": "run_command", "args": {"CommandLine": command, "Cwd": str(repo)}},
        "workspacePaths": [str(repo)],
    }
    if hook_type == "PostToolUse":
        payload["error"] = ""
    return {"hook_type": hook_type, "input_data": payload}


def _hook_shell_cases(adapter: type[BaseAdapter], tool: str, name: str) -> list[Any]:
    return [
        pytest.param(
            adapter,
            lambda r: shell_hook("PostToolUseFailure", tool, OUTLINE, r, error=FAILURE),
            id=f"{name}-failure-error",
        ),
        pytest.param(
            adapter,
            lambda r: shell_hook(
                "PostToolUseFailure",
                tool,
                BATCH,
                r,
                tool_response={"error": f"{FAILURE}\n---\n{OUTAGE}"},
            ),
            id=f"{name}-batch-tool-response-error",
        ),
        pytest.param(
            adapter,
            lambda r: shell_hook(
                "PostToolUse", tool, PIPED, r, tool_response={"stdout": OUTAGE, "stderr": ""}
            ),
            id=f"{name}-piped-stdout",
        ),
    ]


REPORTED_OUTAGES = [
    *_hook_shell_cases(ClaudeCodeAdapter, "Bash", "claude"),
    *_hook_shell_cases(QwenAdapter, "run_shell_command", "qwen"),
    pytest.param(
        CodexHooksAdapter,
        lambda r: shell_hook(
            "PostToolUse",
            "Bash",
            OUTLINE,
            r,
            tool_response={"output": OUTAGE, "exitCode": 2, "status": "failed"},
        ),
        id="codex-hooks-exit-2",
    ),
    pytest.param(
        CodexHooksAdapter,
        lambda r: shell_hook(
            "PostToolUse",
            "Bash",
            PIPED,
            r,
            tool_response={"output": OUTAGE, "exitCode": 0, "status": "completed"},
        ),
        id="codex-hooks-piped",
    ),
    pytest.param(CodexAdapter, lambda r: codex_item(OUTLINE, OUTAGE, r), id="codex-app-server"),
    pytest.param(
        CodexAdapter,
        lambda r: codex_item(BATCH, f"{OUTAGE}\n---\n{OUTAGE}", r),
        id="codex-app-server-batch",
    ),
    pytest.param(
        DroidAdapter,
        lambda r: shell_hook(
            "PostToolUse",
            "Execute",
            PIPED,
            r,
            hook_event_name="PostToolUse",
            tool_response=f"{OUTAGE}\n\n[Process exited with code 0]",
        ),
        id="droid-piped",
    ),
    pytest.param(GrokAdapter, lambda r: grok_hook(OUTLINE, f"exit: 2\n{OUTAGE}", r), id="grok"),
    pytest.param(
        GrokAdapter,
        lambda r: grok_hook(BATCH, f"exit: 2\n{OUTAGE}\n---\n{OUTAGE}", r),
        id="grok-batch",
    ),
]


@pytest.mark.parametrize(("adapter", "native"), REPORTED_OUTAGES)
def test_reported_outage_opens_every_read_in_the_checkout(
    repo: Path, adapter: type[BaseAdapter], native: Native
) -> None:
    data = translate(adapter(), native(repo), repo)
    variables = {"code_index_recoveries": data["canonical_code_index_recovery"]}

    assert navigation_requires_index(read(repo, "src/other.py"), {})
    for path in ("src/app.py", "src/other.py"):
        assert not navigation_requires_index(read(repo, path), variables), path


def test_reported_failure_opens_its_file_without_a_pre_tool_event(repo: Path) -> None:
    """Codex app-server has no pre-tool event for auto-approved commands."""
    error = json.dumps({"error": "invalid_query", "message": "unsupported outline target"})
    data = translate(CodexAdapter(), codex_item(OUTLINE, error, repo), repo)
    variables = {"code_index_recoveries": data["canonical_code_index_recovery"]}

    assert not navigation_requires_index(read(repo, "src/app.py"), variables)
    assert navigation_requires_index(read(repo, "src/other.py"), variables)


@pytest.mark.parametrize(
    ("adapter", "before", "after"),
    [
        pytest.param(
            DroidAdapter,
            lambda r: shell_hook("PreToolUse", "Execute", OUTLINE, r, hook_event_name="PreToolUse"),
            None,
            id="droid-nonzero-emits-no-post-hook",
        ),
        pytest.param(
            AgyAdapter,
            lambda r: agy_hook("PreToolUse", OUTLINE, r),
            lambda r: agy_hook("PostToolUse", OUTLINE, r),
            id="agy-post-hook-without-output",
        ),
    ],
)
def test_unreported_failure_keeps_the_attempted_file_open(
    repo: Path, adapter: type[BaseAdapter], before: Native, after: Native | None
) -> None:
    attempt = translate(adapter(), before(repo), repo)
    outcome = translate(adapter(), after(repo), repo) if after else {}
    variables = {
        "code_index_attempts": attempt["canonical_code_index_attempts"],
        "code_index_verified": outcome.get("canonical_code_index_verified", []),
    }

    assert not navigation_requires_index(read(repo, "src/app.py"), variables)
    assert navigation_requires_index(read(repo, "src/other.py"), variables)
