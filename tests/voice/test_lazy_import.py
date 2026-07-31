"""Regression tests for disabled voice import behavior."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_disabled_voice_imports_do_not_load_torch_or_chatterbox() -> None:
    script = textwrap.dedent(
        """
        import importlib
        import sys

        from gobby.config.voice import VoiceConfig

        config = VoiceConfig()
        assert config.enabled is False

        modules = (
            "gobby.runner",
            "gobby.servers.http",
            "gobby.servers.routes.voice",
            "gobby.servers.websocket.voice",
            "gobby.voice.dep_check",
            "gobby.voice.providers",
            "gobby.voice.stt",
            "gobby.voice.tts",
            "gobby.voice.tts_chatterbox",
        )
        for module in modules:
            importlib.import_module(module)

        heavy_imports = sorted(
            name
            for name in sys.modules
            if name == "torch"
            or name.startswith("torch.")
            or name == "chatterbox"
            or name.startswith("chatterbox.")
        )
        assert heavy_imports == []
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_voice_extra_is_not_advertised() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "voice" not in pyproject["project"]["optional-dependencies"]
