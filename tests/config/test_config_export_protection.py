"""Config export protection at the managed test-sandbox boundary."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, export_config_to_yaml

pytestmark = pytest.mark.unit


def test_managed_sandbox_is_allowed_and_default_production_path_is_blocked(
    temp_dir: Path,
    default_config: DaemonConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gobby_home = temp_dir / "fake-home" / ".gobby"
    original_expanduser = Path.expanduser

    def fake_expanduser(path: Path) -> Path:
        raw = str(path)
        if raw == "~/.gobby":
            return fake_gobby_home
        if raw.startswith("~/.gobby/"):
            return fake_gobby_home / raw.removeprefix("~/.gobby/")
        return original_expanduser(path)

    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(Path, "expanduser", fake_expanduser)
    sandbox_config = fake_gobby_home / "run" / "sandbox" / "isolated-run" / "config.yaml"

    export_config_to_yaml(default_config, str(sandbox_config))

    assert yaml.safe_load(sandbox_config.read_text())["daemon_port"] == 60887
    with pytest.raises(RuntimeError, match="would write to production path"):
        export_config_to_yaml(default_config, config_file=None)
