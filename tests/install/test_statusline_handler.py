"""Tests for the statusline handler script."""

import json
from unittest.mock import patch

import pytest

from gobby.install.shared.hooks.statusline_handler import (
    _extract_payload,
    _log_handler_error,
    _post_to_daemon,
    _read_daemon_port,
    main,
)

pytestmark = pytest.mark.unit


class TestExtractPayload:
    """Test _extract_payload function."""

    def test_extracts_all_fields(self) -> None:
        data = {
            "session_id": "sess-123",
            "model": {"id": "claude-opus-4-6"},
            "cost": {
                "input_tokens": 12345,
                "output_tokens": 6789,
                "cache_creation_tokens": 1000,
                "cache_read_tokens": 5000,
            },
            "context_window": {"size": 200000},
        }
        result = _extract_payload(data)
        assert result is not None
        assert result["session_id"] == "sess-123"
        assert result["model_id"] == "claude-opus-4-6"
        assert result["input_tokens"] == 12345
        assert result["output_tokens"] == 6789
        assert result["cache_creation_tokens"] == 1000
        assert result["cache_read_tokens"] == 5000
        assert result["context_window_size"] == 200000

    def test_returns_none_without_session_id(self) -> None:
        data = {"cost": {"input_tokens": 10}}
        assert _extract_payload(data) is None

    def test_extracts_defaults_without_cost(self) -> None:
        data = {"session_id": "sess-123"}
        result = _extract_payload(data)
        assert result is not None
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0

    def test_defaults_missing_token_fields(self) -> None:
        data = {
            "session_id": "sess-123",
            "cost": {},
        }
        result = _extract_payload(data)
        assert result is not None
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["cache_creation_tokens"] == 0
        assert result["cache_read_tokens"] == 0
        assert result["model_id"] == ""
        assert result["context_window_size"] == 0


class TestReadDaemonPort:
    """Test _read_daemon_port function."""

    def test_reads_port_from_file(self, tmp_path) -> None:
        bootstrap = tmp_path / "bootstrap.yaml"
        bootstrap.write_text("daemon_port: 12345\n")
        with patch("gobby.install.shared.hooks.statusline_handler._BOOTSTRAP_PATH", str(bootstrap)):
            assert _read_daemon_port() == 12345

    def test_default_port_when_missing(self, tmp_path) -> None:
        with patch(
            "gobby.install.shared.hooks.statusline_handler._BOOTSTRAP_PATH",
            str(tmp_path / "nonexistent.yaml"),
        ):
            assert _read_daemon_port() == 60887

    def test_default_port_when_no_key(self, tmp_path) -> None:
        bootstrap = tmp_path / "bootstrap.yaml"
        bootstrap.write_text("other_key: value\n")
        with patch("gobby.install.shared.hooks.statusline_handler._BOOTSTRAP_PATH", str(bootstrap)):
            assert _read_daemon_port() == 60887


class TestMain:
    """Test main() function."""

    def test_parses_valid_json_and_posts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = {
            "session_id": "sess-123",
            "cost": {"input_tokens": 100, "output_tokens": 50},
            "model": {"id": "claude-opus-4-6"},
        }
        monkeypatch.delenv("GOBBY_STATUSLINE_DOWNSTREAM", raising=False)
        with (
            patch("sys.stdin") as mock_stdin,
            patch("gobby.install.shared.hooks.statusline_handler._post_to_daemon") as mock_post,
            patch(
                "gobby.install.shared.hooks.statusline_handler._read_daemon_port",
                return_value=60887,
            ),
        ):
            mock_stdin.read.return_value = json.dumps(data)
            result = main()

        assert result == 0
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == 60887  # port
        posted = json.loads(call_args[0][1])
        assert posted["session_id"] == "sess-123"
        assert posted["input_tokens"] == 100
        assert posted["output_tokens"] == 50

    def test_handles_invalid_json(self) -> None:
        with (
            patch("sys.stdin") as mock_stdin,
            patch("gobby.install.shared.hooks.statusline_handler._log_handler_error") as mock_log,
        ):
            mock_stdin.read.return_value = "not json"
            result = main()
        assert result == 0
        mock_log.assert_called_once()

    def test_handles_empty_stdin(self) -> None:
        with (
            patch("sys.stdin") as mock_stdin,
            patch("gobby.install.shared.hooks.statusline_handler._log_handler_error") as mock_log,
        ):
            mock_stdin.read.return_value = ""
            result = main()
        assert result == 0
        mock_log.assert_called_once()

    def test_forwards_to_downstream(self) -> None:
        data = {
            "session_id": "sess-123",
            "cost": {"input_tokens": 1},
            "model": {"id": "test"},
        }
        with (
            patch("sys.stdin") as mock_stdin,
            patch("gobby.install.shared.hooks.statusline_handler._post_to_daemon"),
            patch(
                "gobby.install.shared.hooks.statusline_handler._read_daemon_port",
                return_value=60887,
            ),
            patch("gobby.install.shared.hooks.statusline_handler._forward_downstream") as mock_fwd,
            patch.dict("os.environ", {"GOBBY_STATUSLINE_DOWNSTREAM": "cship"}, clear=False),
        ):
            mock_stdin.read.return_value = json.dumps(data)
            result = main()

        assert result == 0
        mock_fwd.assert_called_once()
        assert mock_fwd.call_args[0][0] == "cship"

    def test_no_post_without_session_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = {"cost": {"input_tokens": 1}}
        monkeypatch.delenv("GOBBY_STATUSLINE_DOWNSTREAM", raising=False)
        with (
            patch("sys.stdin") as mock_stdin,
            patch("gobby.install.shared.hooks.statusline_handler._post_to_daemon") as mock_post,
        ):
            mock_stdin.read.return_value = json.dumps(data)
            result = main()

        assert result == 0
        mock_post.assert_not_called()


class TestObservability:
    """Test statusline handler bake observability."""

    def test_logs_handler_errors(self, tmp_path) -> None:
        log_path = tmp_path / "statusline_handler_errors.log"

        with patch("gobby.install.shared.hooks.statusline_handler._ERROR_LOG_PATH", str(log_path)):
            _log_handler_error("probe", RuntimeError("boom"))

        text = log_path.read_text()
        assert "statusline_handler_error" in text
        assert "stage=probe" in text
        assert "error=boom" in text

    def test_post_failure_logs_handler_error(self) -> None:
        with (
            patch(
                "gobby.install.shared.hooks.statusline_handler.urllib.request.urlopen",
                side_effect=OSError("daemon unavailable"),
            ),
            patch("gobby.install.shared.hooks.statusline_handler._log_handler_error") as mock_log,
        ):
            _post_to_daemon(60887, b"{}")

        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "post_to_daemon"
