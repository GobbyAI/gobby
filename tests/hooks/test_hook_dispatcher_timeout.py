"""Tests for hook dispatcher timeout values — hold-open pattern support.

Verifies that the hook dispatcher uses appropriate timeout values:
- Short connect timeout (10s) for fast failure when daemon is down
- Long read timeout (600s) for hold-open approvals in web chat
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestHookDispatcherTimeouts:
    def test_httpx_timeout_uses_split_connect_read(self) -> None:
        """httpx path should use Timeout(10.0, read=600.0), not flat 90s."""
        from pathlib import Path

        dispatcher_path = Path("src/gobby/install/shared/hooks/hook_dispatcher.py")
        source = dispatcher_path.read_text()

        # The main hook dispatch httpx path has a comment about LLM-powered hooks.
        # Find that specific timeout= line near the "LLM-powered hooks" comment.
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "LLM-powered hooks" in line or ("timeout=" in line and "90.0" in line):
                # Found the old flat timeout — should be replaced with httpx.Timeout()
                pytest.fail(
                    f"Line {i + 1}: still uses flat timeout=90.0, "
                    "expected httpx.Timeout(10.0, read=600.0)"
                )

        # Verify the replacement exists
        assert "httpx.Timeout(" in source, (
            "No httpx.Timeout() call found — expected Timeout(10.0, read=600.0)"
        )

    def test_curl_max_time_is_600(self) -> None:
        """curl path should use --max-time 600, not 90."""
        from pathlib import Path

        dispatcher_path = Path("src/gobby/install/shared/hooks/hook_dispatcher.py")
        source = dispatcher_path.read_text()

        # Find the --max-time argument value
        lines = source.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == '"--max-time",':
                # Next line should be the value
                next_line = lines[i + 1].strip().strip('",')
                assert next_line == "600", f"curl --max-time is {next_line}, expected 600"
                return
        pytest.fail("--max-time not found in hook_dispatcher.py")
