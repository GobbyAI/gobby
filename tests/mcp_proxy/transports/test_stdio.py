"""Tests for stdio transport environment variable expansion."""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.transports.stdio import StdioTransportConnection, _expand_args
from gobby.utils.env import ENV_VAR_PATTERN, expand_env_mapping, expand_env_variables

pytestmark = pytest.mark.unit


class TestEnvVarPattern:
    """Tests for ENV_VAR_PATTERN regex."""

    def test_matches_simple_var(self) -> None:
        """Matches ${VAR} pattern."""
        match = ENV_VAR_PATTERN.search("${HOME}")
        assert match is not None
        assert match.group(1) == "HOME"
        assert match.group(2) is None

    def test_matches_var_with_default(self) -> None:
        """Matches ${VAR:-default} pattern."""
        match = ENV_VAR_PATTERN.search("${PORT:-8080}")
        assert match is not None
        assert match.group(1) == "PORT"
        assert match.group(2) == "8080"

    def test_matches_var_with_empty_default(self) -> None:
        """Matches ${VAR:-} pattern (empty default)."""
        match = ENV_VAR_PATTERN.search("${EMPTY:-}")
        assert match is not None
        assert match.group(1) == "EMPTY"
        assert match.group(2) == ""

    def test_matches_underscore_in_name(self) -> None:
        """Matches variables with underscores."""
        match = ENV_VAR_PATTERN.search("${MY_VAR_NAME}")
        assert match is not None
        assert match.group(1) == "MY_VAR_NAME"

    def test_matches_numbers_in_name(self) -> None:
        """Matches variables with numbers (not at start)."""
        match = ENV_VAR_PATTERN.search("${VAR123}")
        assert match is not None
        assert match.group(1) == "VAR123"

    def test_no_match_number_start(self) -> None:
        """Does not match variables starting with numbers."""
        match = ENV_VAR_PATTERN.search("${123VAR}")
        assert match is None

    def test_no_match_plain_dollar(self) -> None:
        """Does not match plain $VAR."""
        match = ENV_VAR_PATTERN.search("$VAR")
        assert match is None

    def test_matches_multiple_in_string(self) -> None:
        """Finds all matches in string with multiple vars."""
        matches = list(ENV_VAR_PATTERN.finditer("${A}/${B:-default}"))
        assert len(matches) == 2
        assert matches[0].group(1) == "A"
        assert matches[1].group(1) == "B"
        assert matches[1].group(2) == "default"


class TestExpandEnvVar:
    """Tests for expand_env_variables function."""

    def test_no_var_returns_unchanged(self) -> None:
        """String without variables is returned unchanged."""
        result = expand_env_variables("plain string")
        assert result == "plain string"

    def test_expands_existing_var(self) -> None:
        """Expands variable that exists in environment."""
        with patch.dict(os.environ, {"TEST_VAR": "test_value"}):
            result = expand_env_variables("${TEST_VAR}")
            assert result == "test_value"

    def test_expands_var_in_path(self) -> None:
        """Expands variable embedded in path."""
        with patch.dict(os.environ, {"USER": "alice"}):
            result = expand_env_variables("/home/${USER}/data")
            assert result == "/home/alice/data"

    def test_missing_var_no_default_unchanged(self) -> None:
        """Missing variable without default is left unchanged."""
        with patch.dict(os.environ, {}, clear=True):
            # Ensure the var doesn't exist
            os.environ.pop("NONEXISTENT_VAR", None)
            result = expand_env_variables("${NONEXISTENT_VAR}")
            assert result == "${NONEXISTENT_VAR}"

    def test_missing_var_with_default_uses_default(self) -> None:
        """Missing variable with default uses the default."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MISSING", None)
            result = expand_env_variables("${MISSING:-fallback}")
            assert result == "fallback"

    def test_empty_var_with_default_uses_default(self) -> None:
        """Empty variable with default uses the default."""
        with patch.dict(os.environ, {"EMPTY_VAR": ""}):
            result = expand_env_variables("${EMPTY_VAR:-fallback}")
            assert result == "fallback"

    def test_set_var_ignores_default(self) -> None:
        """Set variable ignores the default."""
        with patch.dict(os.environ, {"SET_VAR": "actual"}):
            result = expand_env_variables("${SET_VAR:-fallback}")
            assert result == "actual"

    def test_empty_default_allowed(self) -> None:
        """Empty default string is valid."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MISSING", None)
            result = expand_env_variables("${MISSING:-}")
            assert result == ""

    def test_multiple_vars_in_string(self) -> None:
        """Expands multiple variables in single string."""
        with patch.dict(os.environ, {"HOST": "localhost", "PORT": "8080"}):
            result = expand_env_variables("http://${HOST}:${PORT}/api")
            assert result == "http://localhost:8080/api"

    def test_mixed_found_and_missing(self) -> None:
        """Handles mix of found and missing variables."""
        with patch.dict(os.environ, {"FOUND": "yes"}, clear=True):
            os.environ.pop("MISSING", None)
            result = expand_env_variables("${FOUND}-${MISSING:-default}")
            assert result == "yes-default"

    def test_default_with_special_chars(self) -> None:
        """Default value can contain special characters."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("VAR", None)
            result = expand_env_variables("${VAR:-http://localhost:8080}")
            assert result == "http://localhost:8080"

    def test_nested_braces_not_supported(self) -> None:
        """Nested braces are not supported (partial match).

        The regex pattern [^}]* captures everything up to the first closing brace,
        so ${OUTER:-${INNER}} matches with default="${INNER" and leaves a trailing }.
        When OUTER is unset, the substitution uses the default "${INNER" plus the
        remaining "}" from the original string, resulting in "${INNER}".
        """
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OUTER", None)
            result = expand_env_variables("${OUTER:-${INNER}}")
            # Default is "${INNER" (up to first }), plus trailing "}" = "${INNER}"
            assert result == "${INNER}"


class TestExpandEnvDict:
    """Tests for _expand_env_dict function."""

    def test_none_returns_none(self) -> None:
        """None input returns None."""
        result = expand_env_mapping(None)
        assert result is None

    def test_empty_dict_returns_empty(self) -> None:
        """Empty dict returns empty dict."""
        result = expand_env_mapping({})
        assert result == {}

    def test_expands_all_values(self) -> None:
        """Expands variables in all dict values."""
        with patch.dict(os.environ, {"USER": "alice", "HOME": "/home/alice"}):
            env = {
                "USERNAME": "${USER}",
                "DATA_DIR": "${HOME}/data",
                "PLAIN": "no_vars",
            }
            result = expand_env_mapping(env)
            assert result == {
                "USERNAME": "alice",
                "DATA_DIR": "/home/alice/data",
                "PLAIN": "no_vars",
            }

    def test_preserves_keys(self) -> None:
        """Keys are not modified, only values."""
        with patch.dict(os.environ, {"KEY": "value"}):
            env = {"${KEY}": "${KEY}"}
            result = expand_env_mapping(env)
            assert "${KEY}" in result  # Key unchanged
            assert result["${KEY}"] == "value"  # Value expanded


class TestExpandArgs:
    """Tests for _expand_args function."""

    def test_none_returns_none(self) -> None:
        """None input returns None."""
        result = _expand_args(None)
        assert result is None

    def test_empty_list_returns_empty(self) -> None:
        """Empty list returns empty list."""
        result = _expand_args([])
        assert result == []

    def test_expands_all_args(self) -> None:
        """Expands variables in all list items."""
        with patch.dict(os.environ, {"PORT": "3000", "HOST": "localhost"}):
            args = ["--port", "${PORT}", "--host", "${HOST}"]
            result = _expand_args(args)
            assert result == ["--port", "3000", "--host", "localhost"]

    def test_preserves_order(self) -> None:
        """List order is preserved."""
        with patch.dict(os.environ, {"A": "1", "B": "2", "C": "3"}):
            args = ["${A}", "${B}", "${C}"]
            result = _expand_args(args)
            assert result == ["1", "2", "3"]

    def test_mixed_plain_and_vars(self) -> None:
        """Handles mix of plain strings and variables."""
        with patch.dict(os.environ, {"VAR": "value"}):
            args = ["plain", "${VAR}", "another"]
            result = _expand_args(args)
            assert result == ["plain", "value", "another"]


@pytest.mark.integration
class TestIntegrationScenarios:
    """Integration tests for realistic usage scenarios."""

    def test_mcp_server_config_expansion(self) -> None:
        """Simulates typical MCP server config expansion."""
        with patch.dict(
            os.environ,
            {
                "MCP_API_KEY": "sk-test-key",
                "MCP_PORT": "3001",
            },
        ):
            args = ["--api-key", "${MCP_API_KEY}", "--port", "${MCP_PORT:-3000}"]
            env = {
                "NODE_ENV": "production",
                "API_KEY": "${MCP_API_KEY}",
            }

            expanded_args = _expand_args(args)
            expanded_env = expand_env_mapping(env)

            assert expanded_args == ["--api-key", "sk-test-key", "--port", "3001"]
            assert expanded_env == {
                "NODE_ENV": "production",
                "API_KEY": "sk-test-key",
            }

    def test_fallback_defaults_when_vars_missing(self) -> None:
        """Uses defaults when environment variables are not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Clear any potentially set vars
            for var in ["CUSTOM_HOST", "CUSTOM_PORT"]:
                os.environ.pop(var, None)

            args = ["--host", "${CUSTOM_HOST:-127.0.0.1}", "--port", "${CUSTOM_PORT:-8080}"]
            result = _expand_args(args)

            assert result == ["--host", "127.0.0.1", "--port", "8080"]

    def test_partial_expansion(self) -> None:
        """Some vars expand, others use defaults or stay unchanged."""
        with patch.dict(os.environ, {"SET_VAR": "value"}, clear=True):
            os.environ.pop("UNSET_WITH_DEFAULT", None)
            os.environ.pop("UNSET_NO_DEFAULT", None)

            args = [
                "${SET_VAR}",
                "${UNSET_WITH_DEFAULT:-default}",
                "${UNSET_NO_DEFAULT}",
            ]
            result = _expand_args(args)

            assert result == ["value", "default", "${UNSET_NO_DEFAULT}"]


class TestStdioCleanup:
    @pytest.mark.asyncio
    async def test_disconnect_closes_original_errlog_handle_only(self) -> None:
        config = MCPServerConfig(
            project_id="project-1",
            name="stdio-test",
            transport="stdio",
            command="node",
        )
        connection = StdioTransportConnection(config)
        original_handle = MagicMock()
        replacement_handle = MagicMock()

        class ReplacingClientContext:
            async def __aexit__(self, *_args: object) -> None:
                connection._stdio_errlog_handle = replacement_handle

        connection._stdio_errlog_handle = original_handle
        connection._client_context = ReplacingClientContext()  # type: ignore[assignment]

        await connection.disconnect()

        original_handle.close.assert_called_once_with()
        replacement_handle.close.assert_not_called()
        assert connection._stdio_errlog_handle is replacement_handle

    @pytest.mark.asyncio
    async def test_cleanup_connect_attempt_logs_structured_cleanup_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = MCPServerConfig(
            project_id="project-1",
            name="stdio-test",
            transport="stdio",
            command="node",
        )
        connection = StdioTransportConnection(config)

        class FailingClientContext:
            async def __aexit__(self, *_args: object) -> None:
                raise RuntimeError("boom")

        connection._client_context = FailingClientContext()  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.transports.stdio"):
            await connection._cleanup_connect_attempt(client_entered=True)

        record = next(
            record for record in caplog.records if "Error during client cleanup" in record.message
        )
        assert record.server == "stdio-test"
        assert record.cleanup_stage == "client"
