"""Tests for Grok CLI hook installer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _supported_ghook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.cli.installers.grok.get_ghook_version", lambda: "0.7.1")


def _write_grok_template(install_dir: Path) -> None:
    template_dir = install_dir / "grok"
    template_dir.mkdir(parents=True)
    (template_dir / "hooks-template.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                    "PostCompact": [{"hooks": [{"type": "command", "command": "legacy"}]}],
                }
            }
        ),
        encoding="utf-8",
    )


def test_current_grok_hooks_are_matcherless_canonical_and_standard_timeout() -> None:
    template_path = Path(__file__).parents[3] / "src/gobby/install/grok/hooks-template.json"
    hooks = json.loads(template_path.read_text(encoding="utf-8"))["hooks"]

    for event_name in ("PermissionDenied", "StopFailure", "SubagentStart", "SubagentStop"):
        assert "matcher" not in hooks[event_name][0]
        assert hooks[event_name][0]["hooks"][0]["timeout"] == 120
    assert "SubagentEnd" not in hooks


@pytest.mark.parametrize("installed_version", [None, "0.4.9", "not-a-version"])
def test_install_grok_refuses_unsupported_ghook_without_side_effects(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_version: str | None,
) -> None:
    from gobby.cli.installers.grok import install_grok

    project_dir = temp_dir / "project"
    project_dir.mkdir()
    monkeypatch.setattr("gobby.cli.installers.grok.get_ghook_version", lambda: installed_version)

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.cli.installers.grok.get_install_dir") as get_install_dir,
        patch("gobby.cli.installers.grok.install_global_hooks") as install_global_hooks,
        patch("gobby.cli.installers.grok.install_shared_content") as install_shared_content,
    ):
        result = install_grok(project_dir)

    assert result["success"] is False
    assert "Grok hooks require ghook >= 0.5.0" in result["error"]
    assert "gobby update" in result["error"]
    get_install_dir.assert_not_called()
    install_global_hooks.assert_not_called()
    install_shared_content.assert_not_called()
    assert not (temp_dir / ".grok").exists()


def test_managed_ghook_pin_satisfies_grok_support_floor() -> None:
    from gobby.cli.installers.grok import _MIN_GHOOK_VERSION_FOR_GROK
    from gobby.install.bin_freshness_models import is_at_least_version
    from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

    assert is_at_least_version(
        MANAGED_BIN_VERSION_PINS["ghook"],
        _MIN_GHOOK_VERSION_FOR_GROK,
    )


def test_install_grok_writes_native_hook_file(
    temp_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gobby.cli._install_prompts import _echo_install_details
    from gobby.cli.installers.grok import install_grok

    install_dir = temp_dir / "install"
    _write_grok_template(install_dir)
    project_dir = temp_dir / "project"
    project_dir.mkdir()

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.cli.installers.grok.get_install_dir", return_value=install_dir),
        patch("gobby.cli.installers.grok.install_global_hooks"),
        patch(
            "gobby.cli.installers.grok.install_shared_content",
            return_value={"agents": [], "plugins": []},
        ),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
    ):
        result = install_grok(project_dir, hook_timeout_seconds=150)

    hook_file = temp_dir / ".grok" / "hooks" / "gobby.json"
    assert result["success"] is True
    assert result["config_path"] == str(hook_file)
    assert result["hooks_installed"] == ["SessionStart", "PreToolUse", "Stop", "PostCompact"]

    config = json.loads(hook_file.read_text(encoding="utf-8"))
    assert (
        config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=grok --type=session_start"
    )
    assert config["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 150
    assert (
        config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        == "/Users/test/.gobby/bin/ghook --gobby-owned --cli=grok --type=pre_tool_use"
    )
    assert config["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("--cli=grok --type=stop")
    assert config["hooks"]["PostCompact"][0]["hooks"][0]["command"].endswith(
        "--cli=grok --type=post_compact"
    )

    grok_config_file = temp_dir / ".grok" / "config.toml"
    grok_config = tomlkit.parse(grok_config_file.read_text(encoding="utf-8"))
    assert grok_config["compat"]["claude"]["hooks"] is False
    assert result["grok_claude_hooks_disabled"] is True
    assert result["grok_config_backup_path"] is None
    assert not list(grok_config_file.parent.glob("config.toml.*.backup"))

    _echo_install_details(result)
    output = capsys.readouterr().out
    assert "Disabled Grok Claude-hook compatibility in ~/.grok/config.toml" in output
    assert "native Grok hooks remain in ~/.grok/hooks/gobby.json" in output


def test_install_grok_disables_existing_claude_hook_compat_and_backs_up(
    temp_dir: Path,
) -> None:
    from gobby.cli.installers.grok import install_grok

    install_dir = temp_dir / "install"
    _write_grok_template(install_dir)
    project_dir = temp_dir / "project"
    project_dir.mkdir()

    grok_config_file = temp_dir / ".grok" / "config.toml"
    grok_config_file.parent.mkdir(parents=True)
    original_config = '[ui]\nyolo = true\n\n[compat.claude]\nhooks = true\nrules = "keep"\n'
    grok_config_file.write_text(original_config, encoding="utf-8")

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.cli.installers.grok.get_install_dir", return_value=install_dir),
        patch("gobby.cli.installers.grok.install_global_hooks"),
        patch(
            "gobby.cli.installers.grok.install_shared_content",
            return_value={"agents": [], "plugins": []},
        ),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
        patch("gobby.cli.installers.grok.time.time", return_value=1234567890),
    ):
        result = install_grok(project_dir)

    backup_file = temp_dir / ".grok" / "config.toml.1234567890.backup"
    assert result["success"] is True
    assert result["grok_config_backup_path"] == str(backup_file)
    assert backup_file.read_text(encoding="utf-8") == original_config

    grok_config = tomlkit.parse(grok_config_file.read_text(encoding="utf-8"))
    assert grok_config["ui"]["yolo"] is True
    assert grok_config["compat"]["claude"]["hooks"] is False
    assert grok_config["compat"]["claude"]["rules"] == "keep"
    assert any(message.startswith("Backed up Grok config: ") for message in result["messages"])


def test_install_grok_leaves_already_disabled_claude_hook_compat_unmodified(
    temp_dir: Path,
) -> None:
    from gobby.cli.installers.grok import install_grok

    install_dir = temp_dir / "install"
    _write_grok_template(install_dir)
    project_dir = temp_dir / "project"
    project_dir.mkdir()

    grok_config_file = temp_dir / ".grok" / "config.toml"
    grok_config_file.parent.mkdir(parents=True)
    original_config = '[models]\ndefault = "grok-build"\n\n[compat.claude]\nhooks = false\n'
    grok_config_file.write_text(original_config, encoding="utf-8")

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.cli.installers.grok.get_install_dir", return_value=install_dir),
        patch("gobby.cli.installers.grok.install_global_hooks"),
        patch(
            "gobby.cli.installers.grok.install_shared_content",
            return_value={"agents": [], "plugins": []},
        ),
        patch(
            "gobby.cli.installers.hook_commands.resolve_native_bin_or_default",
            return_value="/Users/test/.gobby/bin/ghook",
        ),
        patch("gobby.cli.installers.grok.time.time", return_value=1234567890),
    ):
        result = install_grok(project_dir)

    assert result["success"] is True
    assert result["grok_claude_hooks_already_disabled"] is True
    assert result["grok_config_backup_path"] is None
    assert grok_config_file.read_text(encoding="utf-8") == original_config
    assert not list(grok_config_file.parent.glob("config.toml.*.backup"))


def test_uninstall_grok_removes_gobby_hook_file(temp_dir: Path) -> None:
    from gobby.cli.installers.grok import uninstall_grok

    hook_file = temp_dir / ".grok" / "hooks" / "gobby.json"
    hook_file.parent.mkdir(parents=True)
    hook_file.write_text(
        json.dumps({"hooks": {"SessionStart": [], "PreToolUse": []}}),
        encoding="utf-8",
    )

    with patch.object(Path, "home", return_value=temp_dir):
        result = uninstall_grok(temp_dir)

    assert result["success"] is True
    assert result["hooks_removed"] == ["SessionStart", "PreToolUse"]
    assert result["files_removed"] == [str(hook_file)]
    assert not hook_file.exists()


def test_bundled_template_matchers_are_valid_grok_regexes() -> None:
    """Grok treats ``matcher`` as a regular expression; ``*`` alone never matches."""
    import re

    import gobby.install

    template = Path(gobby.install.__file__).parent / "grok" / "hooks-template.json"
    hooks = json.loads(template.read_text())["hooks"]

    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        assert all("matcher" not in group for group in hooks[event]), event
    for groups in hooks.values():
        for group in groups:
            matcher = group.get("matcher")
            if matcher is not None:
                re.compile(matcher)
