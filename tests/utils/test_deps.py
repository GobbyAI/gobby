import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils import deps


def test_run_cmd():
    # Success
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="test output\n")
        assert deps._run_cmd(["echo", "test"]) == "test output"

    # Exceptions
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert deps._run_cmd(["invalid_command"]) is None
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5)):
        assert deps._run_cmd(["invalid_command"]) is None


def test_get_gobby_version():
    with patch("gobby.utils.version.get_version", return_value="1.0.0"):
        assert deps.get_gobby_version() == "1.0.0"
    with patch("gobby.utils.version.get_version", side_effect=Exception):
        assert deps.get_gobby_version() is None


def test_get_gcode_version(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gcode-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.2.1")
        assert deps.get_gcode_version() == "0.2.1"

    # Fallback to CLI
    with patch.object(Path, "home", return_value=tmp_path):
        stamp.unlink()
        with patch("gobby.utils.deps._run_cmd", return_value="gcode 0.2.2"):
            assert deps.get_gcode_version() == "0.2.2"
        with patch("gobby.utils.deps._run_cmd", return_value=None):
            assert deps.get_gcode_version() is None


def test_get_gsqz_version(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gsqz-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("1.1.0")
        assert deps.get_gsqz_version() == "1.1.0"

    # Fallback to CLI
    with patch.object(Path, "home", return_value=tmp_path):
        stamp.unlink()
        with patch("gobby.utils.deps._run_cmd", return_value="gsqz 1.1.1"):
            assert deps.get_gsqz_version() == "1.1.1"
        with patch("gobby.utils.deps._run_cmd", return_value=None):
            assert deps.get_gsqz_version() is None


def test_get_ghook_version(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".ghook-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.2.0")
        assert deps.get_ghook_version() == "0.2.0"

    with patch.object(Path, "home", return_value=tmp_path):
        stamp.unlink()
        ghook = tmp_path / ".gobby" / "bin" / "ghook"
        ghook.write_text("")
        ghook.chmod(0o755)
        with patch("gobby.utils.deps._run_cmd", return_value="ghook 0.2.1"):
            assert deps.get_ghook_version() == "0.2.1"
        with patch("gobby.utils.deps._run_cmd", return_value=None):
            assert deps.get_ghook_version() is None


def test_get_gloc_version(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gloc-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.1.1")
        assert deps.get_gloc_version() == "0.1.1"

    with patch.object(Path, "home", return_value=tmp_path):
        stamp.unlink()
        gloc = tmp_path / ".gobby" / "bin" / "gloc"
        gloc.write_text("")
        gloc.chmod(0o755)
        with patch("gobby.utils.deps._run_cmd", return_value="gloc 0.1.2"):
            assert deps.get_gloc_version() == "0.1.2"
        with patch("gobby.utils.deps._run_cmd", return_value=None):
            assert deps.get_gloc_version() is None


def test_get_claude_code_version():
    with patch("gobby.utils.deps._run_cmd", return_value="claude 1.0.12"):
        assert deps.get_claude_code_version() == "1.0.12"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_claude_code_version() is None


def test_get_gemini_cli_version():
    with patch("gobby.utils.deps._run_cmd", return_value="gemini 2.1.0"):
        assert deps.get_gemini_cli_version() == "2.1.0"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_gemini_cli_version() is None


def test_get_codex_cli_version():
    with patch("gobby.utils.deps._run_cmd", return_value="codex 3.0.0"):
        assert deps.get_codex_cli_version() == "3.0.0"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_codex_cli_version() is None


def test_coding_cli_hooks_status(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        claude = tmp_path / ".claude" / "settings.json"
        gemini = tmp_path / ".gemini" / "settings.json"

        claude.parent.mkdir()
        gemini.parent.mkdir()

        claude.write_text("hook_dispatcher.py")
        gemini.write_text("other text")

        result = deps.get_coding_cli_hooks_status()
        assert result["claude"] is True
        assert result["gemini"] is False
        assert result["codex"] is False


def test_check_hooks_in_file(tmp_path):
    f = tmp_path / "settings.json"
    assert deps._check_hooks_in_file(f) is False
    f.write_text("hook_dispatcher.py")
    assert deps._check_hooks_in_file(f) is True


def test_external_tools():
    with patch("gobby.utils.deps._run_cmd", return_value="tmux 3.4"):
        assert deps.get_tmux_version() == "3.4"
    with patch("gobby.utils.deps._run_cmd", return_value="Docker version 27.1.1, build"):
        assert deps.get_docker_version() == "27.1.1"
    with patch("gobby.utils.deps._run_cmd", return_value="info"):
        assert deps.get_docker_running() is True
    with patch("gobby.utils.deps._run_cmd", return_value="git version 2.44.0"):
        assert deps.get_git_version() == "2.44.0"
    with patch("gobby.utils.deps._run_cmd", return_value="v22.1.0"):
        assert deps.get_node_version() == "22.1.0"


def test_tailscale_info():
    with patch("shutil.which", return_value=False):
        assert deps.get_tailscale_info() is None

    def mock_run(cmd, **kwargs):
        if cmd == ["tailscale", "version"]:
            return "1.66.4\nother"
        if cmd == ["tailscale", "status", "--json"]:
            return json.dumps({"Self": {"DNSName": "test.hostname."}})
        if cmd == ["tailscale", "serve", "status", "--json"]:
            return json.dumps({"Web": {"443": {"/": "backend"}}, "AllowFunnel": {"443": True}})
        return None

    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", side_effect=mock_run),
    ):
        info = deps.get_tailscale_info()
        assert info is not None
        assert info["version"] == "1.66.4"
        assert info["hostname"] == "test.hostname"
        assert info["serving"] == {"443": {"/": "backend"}}
        assert info["funnel"] is True


def test_tailscale_info_exceptions():
    def mock_run(cmd, **kwargs):
        if cmd == ["tailscale", "version"]:
            return "bad version format"
        if cmd == ["tailscale", "status", "--json"]:
            return "invalid json"
        if cmd == ["tailscale", "serve", "status", "--json"]:
            return "invalid json"
        return None

    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", side_effect=mock_run),
    ):
        info = deps.get_tailscale_info()
        assert info is not None
        assert info["version"] == "bad version format"
        assert info["hostname"] is None
        assert info["serving"] == {}


def test_ollama_info():
    with patch("shutil.which", return_value=False):
        assert deps.get_ollama_info() is None

    def mock_run(cmd, **kwargs):
        if cmd == ["ollama", "--version"]:
            return "ollama version is 0.1.30"
        return "list output"

    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", side_effect=mock_run),
    ):
        info = deps.get_ollama_info()
        assert info is not None
        assert info["version"] == "0.1.30"
        assert info["running"] is True


def test_ollama_info_exception():
    def mock_run(cmd, **kwargs):
        if cmd == ["ollama", "--version"]:
            return "weird"
        return None

    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", side_effect=mock_run),
    ):
        info = deps.get_ollama_info()
        assert info is not None
        assert info["version"] == "weird"
        assert info["running"] is False


def test_lmstudio_info():
    with patch("shutil.which", return_value=False):
        assert deps.get_lmstudio_info() is None
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value="Server is running"),
    ):
        assert deps.get_lmstudio_info() == {"running": True}
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value=None),
    ):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="server RUNNING")
            assert deps.get_lmstudio_info() == {"running": True}


def test_lmstudio_info_exception():
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value=None),
    ):
        with patch("subprocess.run", side_effect=Exception):
            assert deps.get_lmstudio_info() == {"running": False}


@pytest.mark.unit
def test_get_configured_embedding_provider() -> None:
    config = MagicMock()
    config.database_path = "/tmp/gobby-hub.db"
    config.embeddings.api_base = None
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = MagicMock()
    store = MagicMock()
    store.get.return_value = "lmstudio"

    with (
        patch("gobby.config.app.load_config", return_value=config),
        patch("gobby.storage.database.LocalDatabase", return_value=db_ctx),
        patch("gobby.storage.config_store.ConfigStore", return_value=store),
    ):
        assert deps.get_configured_embedding_provider() == "lmstudio"


@pytest.mark.unit
def test_get_configured_embedding_provider_falls_back_to_api_base() -> None:
    config = MagicMock()
    config.database_path = "/tmp/gobby-hub.db"
    config.embeddings.api_base = "http://localhost:1234/v1"

    with (
        patch("gobby.config.app.load_config", return_value=config),
        patch("gobby.storage.database.LocalDatabase", side_effect=RuntimeError("db unavailable")),
    ):
        assert deps.get_configured_embedding_provider() == "lmstudio"


def test_check_config_mismatches():
    config = MagicMock()
    config.llm_providers.claude = True
    config.llm_providers.codex = True
    config.llm_providers.gemini = True
    config.embeddings.api_base = "http://localhost:1234/v1"

    with patch("shutil.which", return_value=False):
        issues = deps.check_config_mismatches(config)
        assert len(issues) == 4
        assert issues[0]["subsystem"] == "Claude Code"
        assert issues[1]["subsystem"] == "Codex"
        assert issues[2]["subsystem"] == "Gemini"
        assert issues[3]["subsystem"] == "LM Studio"

    config.embeddings.api_base = "http://localhost:11434/v1"
    with patch("shutil.which", return_value=False):
        issues = deps.check_config_mismatches(config)
        assert len(issues) == 4
        assert issues[3]["subsystem"] == "Ollama"


def test_collect_all_deps():
    with (
        patch("gobby.utils.deps.get_gobby_version", return_value="1"),
        patch("gobby.utils.deps.get_gcode_version", return_value="2"),
        patch("gobby.utils.deps.get_gsqz_version", return_value="3"),
        patch("gobby.utils.deps.get_ghook_version", return_value="3.5"),
        patch("gobby.utils.deps.get_gloc_version", return_value="3.6"),
        patch("gobby.utils.deps.get_claude_code_version", return_value="4"),
        patch("gobby.utils.deps.get_gemini_cli_version", return_value="5"),
        patch("gobby.utils.deps.get_codex_cli_version", return_value="6"),
        patch("gobby.utils.deps.get_coding_cli_hooks_status", return_value={}),
        patch("gobby.utils.deps.get_tmux_version", return_value="7"),
        patch("gobby.utils.deps.get_docker_version", return_value="8"),
        patch("gobby.utils.deps.get_docker_running", return_value=True),
        patch("gobby.utils.deps.get_git_version", return_value="9"),
        patch("gobby.utils.deps.get_node_version", return_value="10"),
        patch("gobby.utils.deps.get_tailscale_info", return_value={}),
        patch("gobby.utils.deps.get_configured_embedding_provider", return_value="lmstudio"),
        patch("gobby.utils.deps.get_ollama_info", return_value={}),
        patch("gobby.utils.deps.get_lmstudio_info", return_value={}),
    ):
        res = deps.collect_all_deps()
        assert res["gobby"]["gobby"] == "1"
        assert res["gobby"]["ghook"] == "3.5"
        assert res["gobby"]["gloc"] == "3.6"
        assert res["dependencies"]["docker_running"] is True
        assert res["dependencies"]["embeddings_provider"] == "lmstudio"


def test_file_read_exceptions(tmp_path):
    with patch("pathlib.Path.read_text", side_effect=OSError):
        with patch.object(Path, "home", return_value=tmp_path):
            stamp = tmp_path / ".gobby" / "bin" / ".gcode-version"
            stamp.parent.mkdir(parents=True)
            stamp.touch()
            with patch("gobby.utils.deps._run_cmd", return_value=None):
                assert deps.get_gcode_version() is None

            sqz = tmp_path / ".gobby" / "bin" / ".gsqz-version"
            sqz.touch()
            with patch("gobby.utils.deps._run_cmd", return_value=None):
                assert deps.get_gsqz_version() is None

    with patch("pathlib.Path.read_text", side_effect=Exception):
        with patch.object(Path, "home", return_value=tmp_path):
            f = tmp_path / "settings.json"
            f.touch()
            assert deps._check_hooks_in_file(f) is False


def test_regex_exceptions():
    with patch("gobby.utils.deps._run_cmd", return_value="weirdformat"):
        assert deps.get_tmux_version() == "weirdformat"
        assert deps.get_docker_version() == "weirdformat"
        assert deps.get_git_version() == "weirdformat"
    with patch("gobby.utils.deps._run_cmd", return_value="   "):
        assert deps.get_node_version() == "   "
