"""The handoff continuation seat launches with exactly the approved tool allowlist.

The live seat is an admitted e2e fixture and skips without its grant. This unit test pins its
launch arguments without a provider, so the allowlist is checked on every run. The allowlist is
documented in docs/development/composer-live-proof.md.
"""

from pathlib import Path

import pytest

from tests.e2e.test_handoff_continuation_e2e import _SCENARIOS, _seat_command

pytestmark = pytest.mark.unit

_BINARY = Path("/fixture/bin/provider")
_GOBBY_PROXY_TOOLS = ["mcp__gobby__call_tool", "mcp__gobby__get_tool_schema"]
_CODEX_FLAGS = [
    "-c",
    "mcp_servers.gobby.enabled=true",
    "--sandbox",
    "read-only",
    "--ask-for-approval",
    "never",
]


@pytest.mark.parametrize(
    ("scenario", "builtin_tools"),
    [("compact", ""), ("clear", ""), ("restart_hold", "")],
)
def test_claude_seat_disables_builtins_and_allows_only_the_gobby_proxy_tools(
    scenario: str, builtin_tools: str
) -> None:
    command = _seat_command("claude", _BINARY, scenario)
    allowed = [*_GOBBY_PROXY_TOOLS]
    assert command[0] == str(_BINARY)
    assert command[2:] == ["--tools", builtin_tools, "--allowedTools", *allowed]


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_codex_seat_enables_gobby_under_the_read_only_sandbox(scenario: str) -> None:
    command = _seat_command("codex", _BINARY, scenario)
    assert command[:-1] == [str(_BINARY), *_CODEX_FLAGS]
