"""UI mode resolution helpers shared by daemon and UI commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from gobby.cli.utils import find_web_dir

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig

ConfiguredUIMode = Literal["auto", "dev", "production"]
EffectiveUIMode = Literal["dev", "production"]


@dataclass(frozen=True)
class UIModeResolution:
    configured: ConfiguredUIMode
    effective: EffectiveUIMode
    source_web_dir: Path | None = None

    @property
    def display(self) -> str:
        if self.configured == "auto":
            return f"auto -> {self.effective}"
        return self.configured


def resolve_ui_mode(config: DaemonConfig) -> UIModeResolution:
    """Resolve configured UI mode to the startup-time effective mode."""
    ui_config = getattr(config, "ui", None)
    configured = getattr(ui_config, "mode", "auto")
    if configured == "dev":
        return UIModeResolution(
            configured="dev",
            effective="dev",
            source_web_dir=find_web_dir(config, require_source=True),
        )
    if configured == "production":
        return UIModeResolution(configured="production", effective="production")

    source_web_dir = find_web_dir(config, require_source=True)
    if source_web_dir is not None:
        return UIModeResolution(
            configured="auto",
            effective="dev",
            source_web_dir=source_web_dir,
        )
    return UIModeResolution(configured="auto", effective="production")
