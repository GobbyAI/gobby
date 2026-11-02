"""Hub-backed Tailscale exposure intent, read and recorded through ConfigStore."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import click
from pydantic import ValidationError

from gobby.cli.config_writes import apply_cas_config_patch
from gobby.config.app import DaemonConfig, McpExposureConfig, UiExposureConfig
from gobby.config.bootstrap_io import bootstrap_path as default_bootstrap_path
from gobby.storage.config_mutations import ConfigPatch
from gobby.storage.config_repository import ConfigReadSnapshot
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.runtime import runtime_hub_database
from gobby.ui_exposure import UiExposeResult, get_exposure_status


def exposure_from_values(
    values: Mapping[str, object],
) -> tuple[UiExposureConfig, McpExposureConfig]:
    """Build the exposure sections from flattened hub config values, defaulting unset keys."""
    config = DaemonConfig.model_validate(
        {
            "ui_exposure": {
                "mode": values.get("ui_exposure.mode", "off"),
                "port": values.get("ui_exposure.port", 443),
            },
            "mcp_exposure": {
                "mode": values.get("mcp_exposure.mode", "off"),
                "port": values.get("mcp_exposure.port", 443),
            },
        }
    )
    return config.ui_exposure, config.mcp_exposure


def validate_exposure_pair(ui: UiExposureConfig, mcp: McpExposureConfig) -> None:
    """Raise ValidationError for a UI and /mcp pair that DaemonConfig refuses.

    Call before any Tailscale mutation so a refused choice never touches serve state.
    """
    DaemonConfig(ui_exposure=ui, mcp_exposure=mcp)


def read_exposure_config(bootstrap_path: Path) -> tuple[UiExposureConfig, McpExposureConfig]:
    """Read the recorded exposure choices from the hub database."""
    with runtime_hub_database(str(bootstrap_path), apply_migrations=False) as database:
        snapshot = ConfigStore(database).read_snapshot()
    return exposure_from_values(snapshot.values)


def exposure_status_lines(daemon_port: int) -> list[str]:
    """Describe the recorded Web UI and /mcp exposures, with their Funnel state."""
    ui_config, mcp_config = read_exposure_config(default_bootstrap_path())
    ui_result, mcp_result = get_exposure_status(daemon_port, ui=ui_config, mcp=mcp_config)
    return [
        _describe_exposure("Web UI", ui_config.mode, ui_result),
        _describe_exposure("MCP", mcp_config.mode, mcp_result),
    ]


def _describe_exposure(label: str, mode: str, result: UiExposeResult) -> str:
    if mode == "off":
        return f"{label} exposure: off"
    if result.funnel is None:
        funnel = "unknown"
    else:
        funnel = "on" if result.funnel else "off"
    if result.degraded_reason is not None:
        return f"{label} exposure: {mode} (degraded: {result.degraded_reason}; Funnel {funnel})"
    return f"{label} exposure: {mode} ({result.url}; Funnel {funnel})"


def record_exposure(
    bootstrap_path: Path,
    *,
    ui: UiExposureConfig | None = None,
    mcp: McpExposureConfig | None = None,
) -> None:
    """Record the given exposure sections after validating the combined state.

    A section left as None keeps its recorded value, so the conflict rules see
    the state the daemon would load.
    """

    def build_patch(snapshot: ConfigReadSnapshot) -> ConfigPatch:
        current_ui, current_mcp = exposure_from_values(snapshot.values)
        next_ui = current_ui if ui is None else ui
        next_mcp = current_mcp if mcp is None else mcp
        try:
            DaemonConfig.model_validate(
                {
                    "ui_exposure": next_ui.model_dump(),
                    "mcp_exposure": next_mcp.model_dump(),
                }
            )
        except ValidationError as exc:
            message = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
            raise click.ClickException(message) from exc
        values: dict[str, object] = {}
        if ui is not None:
            values["ui_exposure.mode"] = ui.mode
            values["ui_exposure.port"] = ui.port
        if mcp is not None:
            values["mcp_exposure.mode"] = mcp.mode
            values["mcp_exposure.port"] = mcp.port
        return ConfigPatch(values=values)

    with runtime_hub_database(str(bootstrap_path), apply_migrations=False) as database:
        store = ConfigStore(database)
        apply_cas_config_patch(
            read_snapshot=store.read_snapshot,
            build_patch=build_patch,
            patch=store.patch,
        )
