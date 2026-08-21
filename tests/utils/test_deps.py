import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
)
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.utils import deps
from gobby.utils.dependency_requirements import DependencyReport, DependencyStatus
from gobby.utils.status import format_status_message


def _patch_config(
    db: HubDatabase,
    values: dict[str, object],
    *,
    secret_store: SecretStore | None = None,
    secrets: dict[str, SecretUpdate] | None = None,
) -> None:
    mutations = ConfigMutations(db, secret_store=secret_store)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(values=values, secrets=secrets or {}),
        source="test",
    )


def _insert_raw_config(db: HubDatabase, values: dict[str, object]) -> None:
    """Seed intentionally corrupt rows that the validated mutation API rejects."""
    for key, value in values.items():
        db.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            VALUES (%s, %s, 'test', FALSE, NOW())
            """,
            (key, json.dumps(value)),
        )


def test_run_cmd() -> None:
    # Success
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="test output\n")
        assert deps._run_cmd(["echo", "test"]) == "test output"

    # Exceptions
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert deps._run_cmd(["invalid_command"]) is None
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5)):
        assert deps._run_cmd(["invalid_command"]) is None


def test_get_gobby_version() -> None:
    with patch("gobby.utils.version.get_version", return_value="1.0.0"):
        assert deps.get_gobby_version() == "1.0.0"
    with patch("gobby.utils.version.get_version", side_effect=Exception):
        assert deps.get_gobby_version() is None


def test_get_gcode_version(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gcode-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.2.1")
        with patch("gobby.utils.deps.resolve_native_bin", return_value=None):
            assert deps.get_gcode_version() == "0.2.1"

    # CLI probe wins over stale stamps.
    with patch.object(Path, "home", return_value=tmp_path):
        with patch("gobby.utils.deps.resolve_native_bin", return_value="/usr/bin/gcode"):
            with patch("gobby.utils.deps.probe_native_bin_version", return_value="0.2.2"):
                assert deps.get_gcode_version() == "0.2.2"
            with patch("gobby.utils.deps.probe_native_bin_version", return_value=None):
                assert deps.get_gcode_version() == "0.2.1"


@pytest.mark.parametrize("installed", [None, Path("/managed/impeccable")])
def test_get_impeccable_version(installed: Path | None) -> None:
    with patch(
        "gobby.cli.install_setup_impeccable.inspect_impeccable_installation",
        return_value=installed,
    ):
        expected = "3.5.0" if installed is not None else None
        assert deps.get_impeccable_version() == expected


def test_get_impeccable_version_rejects_corrupt_or_mismatched_install() -> None:
    from gobby.cli.install_setup_impeccable import ImpeccableInstallError

    with patch(
        "gobby.cli.install_setup_impeccable.inspect_impeccable_installation",
        side_effect=ImpeccableInstallError("corrupt"),
    ):
        assert deps.get_impeccable_version() is None


def test_get_impeccable_version_honors_configured_home_and_rejects_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from gobby.cli import install_setup_impeccable as impeccable
    from gobby.utils.dependency_requirements import IMPECCABLE_RELEASE

    home = tmp_path / "configured-home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    root = home / "tools" / "impeccable"
    generation = root / "3.5.0-generation-test"
    bin_dir = generation / "node_modules" / ".bin"
    impeccable_package = generation / "node_modules" / "impeccable"
    puppeteer_package = generation / "node_modules" / "puppeteer"
    (impeccable_package / "cli" / "bin").mkdir(parents=True)
    (puppeteer_package / "lib" / "puppeteer" / "node").mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    (impeccable_package / "package.json").write_text(
        json.dumps(
            {
                "name": "impeccable",
                "version": "3.5.0",
                "bin": {"impeccable": "cli/bin/cli.js"},
            }
        )
    )
    (puppeteer_package / "package.json").write_text(
        json.dumps(
            {
                "name": "puppeteer",
                "version": impeccable._locked_package_version("puppeteer"),
                "bin": {"puppeteer": "lib/puppeteer/node/cli.js"},
            }
        )
    )
    for target in (
        impeccable_package / "cli" / "bin" / "cli.js",
        puppeteer_package / "lib" / "puppeteer" / "node" / "cli.js",
    ):
        target.write_text("#!/usr/bin/env node\n")
        target.chmod(0o755)
    (bin_dir / "impeccable").symlink_to("../impeccable/cli/bin/cli.js")
    (bin_dir / "puppeteer").symlink_to("../puppeteer/lib/puppeteer/node/cli.js")
    (generation / "receipt.json").write_text(json.dumps(IMPECCABLE_RELEASE.receipt_fields()))
    pointer = root / "3.5.0"
    pointer.symlink_to(generation.name)
    launcher = home / "bin" / "impeccable"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(impeccable._launcher_content(home, pointer))
    launcher.chmod(0o755)
    (home / "bin" / ".impeccable-version").write_text("3.5.0\n")

    assert deps.get_impeccable_version() == "3.5.0"
    (bin_dir / "impeccable").unlink()
    assert deps.get_impeccable_version() is None


def test_get_ghook_version(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".ghook-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.2.0")
        with patch("gobby.utils.deps.resolve_native_bin", return_value=None):
            assert deps.get_ghook_version() == "0.2.0"

    with patch.object(Path, "home", return_value=tmp_path):
        ghook = tmp_path / ".gobby" / "bin" / "ghook"
        ghook.write_text("")
        ghook.chmod(0o755)
        with patch("gobby.utils.deps.probe_native_bin_version", return_value="0.2.1"):
            assert deps.get_ghook_version() == "0.2.1"
        with patch("gobby.utils.deps.probe_native_bin_version", return_value=None):
            assert deps.get_ghook_version() == "0.2.0"


def test_get_gwiki_version(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gwiki-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.1.0")
        with patch("gobby.utils.deps.resolve_native_bin", return_value=None):
            assert deps.get_gwiki_version() == "0.1.0"

    with patch.object(Path, "home", return_value=tmp_path):
        gwiki = tmp_path / ".gobby" / "bin" / "gwiki"
        gwiki.write_text("")
        gwiki.chmod(0o755)
        with patch("gobby.utils.deps.probe_native_bin_version", return_value="0.1.1"):
            assert deps.get_gwiki_version() == "0.1.1"
        with patch("gobby.utils.deps.probe_native_bin_version", return_value=None):
            assert deps.get_gwiki_version() == "0.1.0"


def test_get_gterm_version(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gterm-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.1.0")
        with patch("gobby.utils.deps.resolve_native_bin", return_value=None):
            assert deps.get_gterm_version() == "0.1.0"

    with patch.object(Path, "home", return_value=tmp_path):
        gterm = tmp_path / ".gobby" / "bin" / "gterm"
        gterm.write_text("")
        gterm.chmod(0o755)
        with patch("gobby.utils.deps.probe_native_bin_version", return_value="0.1.1"):
            assert deps.get_gterm_version() == "0.1.1"
        with patch("gobby.utils.deps.probe_native_bin_version", return_value=None):
            assert deps.get_gterm_version() == "0.1.0"


def test_get_gclient_version(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        stamp = tmp_path / ".gobby" / "bin" / ".gclient-version"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("0.1.0")
        with patch("gobby.utils.deps.resolve_native_bin", return_value=None):
            assert deps.get_gclient_version() == "0.1.0"

    with patch.object(Path, "home", return_value=tmp_path):
        gclient = tmp_path / ".gobby" / "bin" / "gclient"
        gclient.write_text("")
        gclient.chmod(0o755)
        with patch("gobby.utils.deps.probe_native_bin_version", return_value="0.1.1"):
            assert deps.get_gclient_version() == "0.1.1"
        with patch("gobby.utils.deps.probe_native_bin_version", return_value=None):
            assert deps.get_gclient_version() == "0.1.0"


def test_get_claude_code_version() -> None:
    with patch("gobby.utils.deps._run_cmd", return_value="claude 1.0.12"):
        assert deps.get_claude_code_version() == "1.0.12"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_claude_code_version() is None


def test_get_codex_cli_version() -> None:
    with patch("gobby.utils.deps._run_cmd", return_value="codex 3.0.0"):
        assert deps.get_codex_cli_version() == "3.0.0"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_codex_cli_version() is None


def test_get_qwen_cli_version() -> None:
    with patch("gobby.utils.deps._run_cmd", return_value="0.15.3"):
        assert deps.get_qwen_cli_version() == "0.15.3"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_qwen_cli_version() is None


def test_get_droid_cli_version() -> None:
    with patch("gobby.utils.deps._run_cmd", return_value="droid 0.106.0"):
        assert deps.get_droid_cli_version() == "0.106.0"
    with patch("gobby.utils.deps._run_cmd", return_value=None):
        assert deps.get_droid_cli_version() is None


def test_coding_cli_hooks_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOBBY_DROID_HOOKS_FILE", raising=False)
    monkeypatch.delenv("GOBBY_HOOKS_DIR", raising=False)

    with patch.object(Path, "home", return_value=tmp_path):
        claude = tmp_path / ".claude" / "settings.json"
        qwen = tmp_path / ".qwen" / "settings.json"
        droid = tmp_path / ".factory" / "hooks.json"

        claude.parent.mkdir()
        qwen.parent.mkdir()
        droid.parent.mkdir(parents=True)

        claude.write_text("ghook --gobby-owned --cli=claude")
        qwen.write_text("ghook --gobby-owned --cli=qwen")
        droid.write_text("ghook --gobby-owned --cli=droid")

        result = deps.get_coding_cli_hooks_status()
        assert result["claude"] is True
        assert result["codex"] is False
        assert result["qwen"] is True
        assert result["droid"] is True


def test_check_hooks_in_file(tmp_path: Path) -> None:
    f = tmp_path / "settings.json"
    assert deps._check_hooks_in_file(f) is False
    f.write_text("ghook --gobby-owned --cli=codex")
    assert deps._check_hooks_in_file(f) is True


def test_external_tools() -> None:
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


def test_tailscale_info() -> None:
    with patch("shutil.which", return_value=False):
        assert deps.get_tailscale_info() is None

    def mock_run(cmd: list[str], **kwargs: Any) -> str | None:
        if cmd == ["tailscale", "version"]:
            return "1.66.4\nother"
        if cmd == ["tailscale", "status", "--json"]:
            return json.dumps({"Self": {"DNSName": "test.hostname."}})
        if cmd == ["tailscale", "serve", "status", "--json"]:
            return json.dumps(
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "test.hostname:443": {
                            "Handlers": {"/": {"Proxy": "http://localhost:60887"}}
                        }
                    },
                    "AllowFunnel": {"test.hostname:443": True},
                }
            )
        return None

    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", side_effect=mock_run),
    ):
        info = deps.get_tailscale_info()
        assert info is not None
        assert info["version"] == "1.66.4"
        assert info["hostname"] == "test.hostname"
        assert info["serving"] == {
            "test.hostname:443": {"Handlers": {"/": {"Proxy": "http://localhost:60887"}}}
        }
        assert info["funnel"] is True


def test_tailscale_info_exceptions() -> None:
    def mock_run(cmd: list[str], **kwargs: Any) -> str | None:
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


def test_ollama_info() -> None:
    with patch("shutil.which", return_value=False):
        assert deps.get_ollama_info() is None

    def mock_run(cmd: list[str], **kwargs: Any) -> str | None:
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


def test_ollama_info_exception() -> None:
    def mock_run(cmd: list[str], **kwargs: Any) -> str | None:
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


def test_lmstudio_info() -> None:
    with patch("shutil.which", return_value=False):
        assert deps.get_lmstudio_info() is None
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value="Server is running"),
    ):
        assert deps.get_lmstudio_info() == {"running": True}
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value="The server is not running"),
    ):
        assert deps.get_lmstudio_info() == {"running": False}
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value=None),
    ):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="server RUNNING")
            assert deps.get_lmstudio_info() == {"running": True}
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="The server is NOT RUNNING",
            )
            assert deps.get_lmstudio_info() == {"running": False}


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(cmd=["lms"], timeout=5), OSError("lms failed")],
)
def test_lmstudio_info_expected_exception(
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.DEBUG, logger="gobby.utils.deps"),
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value=None),
        patch("subprocess.run", side_effect=error),
    ):
        assert deps.get_lmstudio_info() == {"running": False}
    assert caplog.messages == ["Failed to determine LM Studio server status"]
    assert caplog.records[0].exc_info is not None


def test_lmstudio_info_unexpected_exception_propagates() -> None:
    with (
        patch("shutil.which", return_value=True),
        patch("gobby.utils.deps._run_cmd", return_value=None),
        patch("subprocess.run", side_effect=RuntimeError("programming error")),
        pytest.raises(RuntimeError, match="programming error"),
    ):
        deps.get_lmstudio_info()


@pytest.mark.unit
def test_get_configured_embedding_provider_detects_ollama(temp_db: HubDatabase) -> None:
    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_API_BASE_KEY: "http://localhost:11434/v1",
            AI_EMBEDDING_DIM_KEY: 768,
        },
    )

    assert deps.get_configured_embedding_provider(temp_db) == "ollama"


@pytest.mark.unit
def test_get_configured_embedding_provider_detects_lmstudio(temp_db: HubDatabase) -> None:
    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_API_BASE_KEY: "http://localhost:1234/v1",
            AI_EMBEDDING_DIM_KEY: 768,
        },
    )

    assert deps.get_configured_embedding_provider(temp_db) == "lmstudio"


@pytest.mark.unit
def test_get_configured_embedding_provider_detects_embedding_api_key(
    temp_db: HubDatabase,
    mock_machine_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    secret_store = SecretStore(temp_db)
    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_MODEL_KEY: "text-embedding-3-small",
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 1536,
        },
        secret_store=secret_store,
        secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("sk-test")},
    )
    assert deps.get_configured_embedding_provider(temp_db) == "openai"


@pytest.mark.unit
@pytest.mark.parametrize(
    "api_base",
    [
        "https://api.openai.com/v1",
        "https://example.openai.azure.com/openai/deployments/embedding",
        "https://example.services.ai.azure.com/models",
    ],
)
def test_get_configured_embedding_provider_detects_explicit_cloud_api_base(
    api_base: str,
    temp_db: HubDatabase,
    mock_machine_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    secret_store = SecretStore(temp_db)
    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_MODEL_KEY: "text-embedding-3-small",
            AI_EMBEDDING_API_BASE_KEY: api_base,
            AI_EMBEDDING_DIM_KEY: 1536,
        },
        secret_store=secret_store,
        secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("sk-test")},
    )
    assert deps.get_configured_embedding_provider(temp_db) == "openai"


@pytest.mark.unit
def test_get_configured_embedding_provider_strips_resolved_api_key(
    temp_db: HubDatabase,
) -> None:
    secret_store = SecretStore(temp_db)
    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_MODEL_KEY: "text-embedding-3-small",
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 1536,
        },
        secret_store=secret_store,
        secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate(" sk-test ")},
    )

    assert deps.get_configured_embedding_provider(temp_db) == "openai"


@pytest.mark.unit
def test_get_configured_embedding_provider_ignores_whitespace_only_api_key(
    temp_db: HubDatabase,
) -> None:
    secret_store = SecretStore(temp_db)
    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_MODEL_KEY: "text-embedding-3-small",
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 1536,
        },
        secret_store=secret_store,
        secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("   ")},
    )

    assert deps.get_configured_embedding_provider(temp_db) is None


@pytest.mark.unit
def test_get_configured_embedding_provider_returns_none_without_secret(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    _patch_config(
        temp_db,
        {
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 1536,
        },
    )

    assert deps.get_configured_embedding_provider(temp_db) is None


@pytest.mark.unit
def test_get_configured_embedding_provider_detects_disabled_state(temp_db: HubDatabase) -> None:
    _insert_raw_config(
        temp_db,
        {
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 0,
        },
    )

    assert deps.get_configured_embedding_provider(temp_db) == "none"


@pytest.mark.unit
def test_get_configured_embedding_provider_disabled_state_overrides_stale_api_base(
    temp_db: HubDatabase,
) -> None:
    _insert_raw_config(
        temp_db,
        {
            AI_EMBEDDING_API_BASE_KEY: "https://stale.example.test/v1",
            AI_EMBEDDING_DIM_KEY: 0,
        },
    )

    assert deps.get_configured_embedding_provider(temp_db) == "none"


@pytest.mark.unit
def test_get_configured_embedding_provider_ignores_invalid_dim_string(
    temp_db: HubDatabase,
) -> None:
    _insert_raw_config(
        temp_db,
        {
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: "invalid",
        },
    )

    assert deps.get_configured_embedding_provider(temp_db) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("hub database unavailable"),
        BootstrapConfigError("hub backend is invalid"),
        RuntimeError("runtime hub unavailable"),
        OSError("config storage unavailable"),
    ],
)
def test_get_configured_embedding_provider_returns_none_for_storage_errors(
    error: Exception,
) -> None:
    with patch(
        "gobby.storage.config_repository.ConfigRepository",
        side_effect=error,
    ):
        assert deps.get_configured_embedding_provider(MagicMock()) is None


@pytest.mark.unit
def test_get_configured_embedding_provider_reraises_unexpected_errors() -> None:
    with (
        patch(
            "gobby.storage.config_repository.ConfigRepository",
            side_effect=AssertionError("database invariant bug"),
        ),
        pytest.raises(AssertionError, match="database invariant bug"),
    ):
        deps.get_configured_embedding_provider(MagicMock())


def test_check_config_mismatches() -> None:
    config = MagicMock()
    config.chat.candidates = ["claude/sonnet"]
    config.embeddings.api_base = "http://localhost:1234/v1"

    with patch("shutil.which", return_value=False):
        issues = deps.check_config_mismatches(config)
        assert len(issues) == 2
        assert issues[0]["subsystem"] == "Claude Code"
        assert issues[1]["subsystem"] == "LM Studio"

    config.embeddings.api_base = "http://localhost:11434/v1"
    with patch("shutil.which", return_value=False):
        issues = deps.check_config_mismatches(config)
        assert len(issues) == 2
        assert issues[1]["subsystem"] == "Ollama"


def test_check_config_mismatches_ignores_non_string_chat_candidates() -> None:
    config = MagicMock()
    config.chat.candidates = [123, {"provider": "claude"}, "openai/gpt-5"]
    config.embeddings.api_base = None

    with patch("shutil.which", return_value=False):
        issues = deps.check_config_mismatches(config)

    assert issues == []


def test_collect_all_deps() -> None:
    healthy = DependencyStatus(
        state="healthy",
        installed_version="9",
        minimum_version="1",
        expected_version=None,
        path="/bin/tool",
        error=None,
    )
    with (
        patch("gobby.utils.deps.get_gobby_version", return_value="1"),
        patch("gobby.utils.deps.get_gcode_version", return_value="2"),
        patch("gobby.utils.deps.get_ghook_version", return_value="3.5"),
        patch("gobby.utils.deps.get_gwiki_version", return_value="3.7"),
        patch("gobby.utils.deps.get_gterm_version", return_value="0.1.0"),
        patch("gobby.utils.deps.get_gclient_version", return_value="0.1.0"),
        patch("gobby.utils.deps.get_impeccable_version", return_value="3.5.0"),
        patch("gobby.utils.deps.get_claude_code_version", return_value="4"),
        patch("gobby.utils.deps.get_codex_cli_version", return_value="6"),
        patch("gobby.utils.deps.get_droid_cli_version", return_value="6.5"),
        patch("gobby.utils.deps.get_qwen_cli_version", return_value="6.7"),
        patch("gobby.utils.deps.get_coding_cli_hooks_status", return_value={}),
        patch(
            "gobby.utils.deps.collect_dependency_report",
            return_value=DependencyReport(
                runtime={"python": healthy},
                required={"git": healthy},
                optional={},
                services={"docker_running": True},
            ),
        ),
        patch("gobby.utils.deps.get_tailscale_info", return_value={}),
        patch("gobby.utils.deps.get_configured_embedding_provider", return_value="lmstudio"),
        patch("gobby.utils.deps.get_ollama_info", return_value={}),
        patch("gobby.utils.deps.get_lmstudio_info", return_value={}),
    ):
        res = deps.collect_all_deps(MagicMock(), managed_services=True)
        assert res["gobby"]["gobby"] == "1"
        assert res["gobby"]["ghook"] == "3.5"
        assert res["gobby"]["gwiki"] == "3.7"
        assert res["gobby"]["gterm"] == "0.1.0"
        assert res["gobby"]["gclient"] == "0.1.0"
        assert res["gobby"]["impeccable"] == "3.5.0"
        assert res["coding_clis"]["droid"] == "6.5"
        assert res["coding_clis"]["qwen"] == "6.7"
        assert res["services"]["docker_running"] is True
        assert res["dependencies"]["required"]["git"]["state"] == "healthy"
        assert res["integrations"]["embeddings_provider"] == "lmstudio"


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("hub database unavailable"),
        BootstrapConfigError("hub backend is invalid"),
    ],
)
def test_collect_all_deps_degrades_when_embeddings_probe_fails(error: Exception) -> None:
    with (
        patch(
            "gobby.utils.deps.get_configured_embedding_provider",
            side_effect=error,
        ),
        patch(
            "gobby.utils.deps.collect_dependency_report",
            return_value=DependencyReport(
                runtime={},
                required={},
                optional={},
                services={},
            ),
        ),
    ):
        res = deps.collect_all_deps(MagicMock(), managed_services=False)

    error_name = type(error).__name__
    assert res["integrations"]["embeddings_provider"] == {
        "status": "degraded",
        "error": error_name,
    }
    assert f"Embeddings:       degraded ({error_name})" in format_status_message(
        running=True,
        deps_info=res,
    )


def test_file_read_exceptions(tmp_path: Path) -> None:
    with patch("pathlib.Path.read_text", side_effect=OSError):
        with patch.object(Path, "home", return_value=tmp_path):
            stamp = tmp_path / ".gobby" / "bin" / ".gcode-version"
            stamp.parent.mkdir(parents=True)
            stamp.touch()
            with patch("gobby.utils.deps.probe_native_bin_version", return_value=None):
                assert deps.get_gcode_version() is None

    with patch("pathlib.Path.read_text", side_effect=Exception):
        with patch.object(Path, "home", return_value=tmp_path):
            f = tmp_path / "settings.json"
            f.touch()
            assert deps._check_hooks_in_file(f) is False


def test_regex_exceptions() -> None:
    with patch("gobby.utils.deps._run_cmd", return_value="weirdformat"):
        assert deps.get_tmux_version() == "weirdformat"
        assert deps.get_docker_version() == "weirdformat"
        assert deps.get_git_version() == "weirdformat"
    with patch("gobby.utils.deps._run_cmd", return_value="   "):
        assert deps.get_node_version() == "   "
