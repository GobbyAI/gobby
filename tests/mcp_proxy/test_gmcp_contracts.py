"""Keep the native wrapper's embedded contract equal to its Python peer."""

import json
from pathlib import Path

from gobby.mcp_proxy import wait_tools
from gobby.mcp_proxy.terminal_context import TERMINAL_CONTEXT_KEYS
from gobby.utils.local_token import _AGENT_IDENTITY_ENV_HEADERS


def test_wrapper_contract_matches_python_constants() -> None:
    contract_path = Path(__file__).resolve().parents[2] / "crates/gmcp/contracts/mcp_wrapper.v1.json"
    contract = json.loads(contract_path.read_text())
    assert contract == {
        "protocol_version": wait_tools.MCP_WRAPPER_PROTOCOL_VERSION,
        "protocol_version_header": wait_tools.MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
        "stale_error_code": wait_tools.MCP_WRAPPER_STALE_ERROR_CODE,
        "wait_tool_names": list(wait_tools.WAIT_TOOL_NAMES),
        "wait_tool_timeout_seconds": wait_tools.MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
        "extended_timeout_tool_names": list(wait_tools.EXTENDED_TIMEOUT_TOOL_NAMES),
        "extended_tool_timeout_seconds": wait_tools.MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS,
        "http_timeout_buffer_seconds": wait_tools.WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS,
        "wrapper_grace_seconds": wait_tools.WAIT_TOOL_WRAPPER_GRACE_SECONDS,
        "heartbeat_interval_seconds": wait_tools.WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS,
        "terminal_context_keys": list(TERMINAL_CONTEXT_KEYS),
        "agent_identity_env_headers": [list(pair) for pair in _AGENT_IDENTITY_ENV_HEADERS],
    }
