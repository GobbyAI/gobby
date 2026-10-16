"""A `bash <script>` read a privacy prompt blocks no longer wedges its session (#24505).

The incident ran `bash -n`, which #23884 classifies as parse-only without reading the
script, so an executing `bash` reaches the same script-body read here. This file uses
only names that predate the fix, so the same test runs against the unfixed tree, where
the first hook times out holding the session's admission.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks.adapter_execution import run_adapter_hook
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit

# Longer than the script-read bound, so a bounded read fails closed inside it.
_ADAPTER_TIMEOUT = 5.0
_ENVELOPES = ("env-bash", "env-next")


@pytest.mark.asyncio
async def test_blocked_script_body_read_in_normalization_leaves_session_admitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "reset-unowned-files.command"
    script.write_text("#!/usr/bin/env bash\ngit status\n", encoding="utf-8")
    prompt = threading.Event()
    entered = threading.Semaphore(0)
    returned = threading.Semaphore(0)
    path_open = Path.open

    def open_behind_prompt(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self != script.resolve():
            return path_open(self, *args, **kwargs)
        entered.release()
        prompt.wait(timeout=30)
        try:
            return path_open(self, *args, **kwargs)
        finally:
            returned.release()

    monkeypatch.setattr(Path, "open", open_behind_prompt)
    hook_manager = MagicMock()
    hook_manager.handle.return_value = HookResponse(decision="allow")
    command = f"chmod +x {script} && bash {script}"
    try:
        for envelope_id in _ENVELOPES:
            payload = {
                "_platform_session_id": "script-read-session",
                "hook_type": "pre-tool-use",
                "source": "claude",
                "input_data": {
                    "source_event_id": envelope_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "cwd": str(tmp_path),
                },
            }
            result = await run_adapter_hook(
                ClaudeCodeAdapter(), payload, hook_manager, timeout_seconds=_ADAPTER_TIMEOUT
            )
            assert result == {"continue": True}
        # Each envelope's normalization reached the blocked open() of the script.
        for _ in _ENVELOPES:
            assert entered.acquire(blocking=False)
        assert not entered.acquire(blocking=False)
    finally:
        prompt.set()
    for _ in _ENVELOPES:
        assert returned.acquire(timeout=5)
    assert hook_manager.handle.call_count == len(_ENVELOPES)
