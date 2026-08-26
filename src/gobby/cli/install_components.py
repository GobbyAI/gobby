"""Component registry and runners for ``gobby install`` / ``gobby uninstall``.

A component is one independently (re)installable piece of a Gobby install. Bare
``gobby install`` runs the full install; ``gobby install <component>...`` runs only
the named components against an existing install.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import psycopg
from psycopg_pool import PoolTimeout

from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.bootstrap_io import bootstrap_path
from gobby.paths import get_gobby_home
from gobby.storage.hub.protocol import HubDatabase

from ._install_prompts import (
    _run_embedding_install,
    _run_git_hooks_install,
    _run_standard_cli_install,
    _run_standard_cli_uninstall,
    _run_voice_install,
)
from .install_setup import configure_ide_terminals, provision_impeccable
from .install_setup_impeccable import remove_impeccable_runtime
from .install_setup_rtk import (
    RtkInstallStatus,
    disable_rule_if_present,
    reconcile_rtk,
    remove_managed_rtk,
)
from .installers import (
    install_agy,
    install_claude,
    install_codex,
    install_droid,
    install_embedding,
    install_git_hooks,
    install_grok,
    install_qwen,
    uninstall_agy,
    uninstall_claude,
    uninstall_codex,
    uninstall_droid,
    uninstall_grok,
    uninstall_qwen,
)
from .installers.git_hooks import uninstall_git_hooks
from .runtime import CliRuntime

logger = logging.getLogger(__name__)

COMPONENTS: tuple[str, ...] = (
    "claude",
    "codex",
    "grok",
    "qwen",
    "droid",
    "agy",
    "git-hooks",
    "rtk",
    "impeccable",
    "voice",
    "embedding",
    "ide-settings",
)
CLI_COMPONENTS: frozenset[str] = frozenset({"claude", "codex", "grok", "qwen", "droid", "agy"})
# Components that leave something behind to remove; voice, embedding, and
# ide-settings only write configuration and have no uninstall.
UNINSTALLABLE_COMPONENTS: tuple[str, ...] = (
    "claude",
    "codex",
    "grok",
    "qwen",
    "droid",
    "agy",
    "git-hooks",
    "rtk",
    "impeccable",
)

COMPONENT_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "grok": "Grok CLI",
    "qwen": "Qwen CLI",
    "droid": "Droid CLI",
    "agy": "AGY CLI",
    "git-hooks": "Git hooks",
    "rtk": "RTK",
    "impeccable": "Impeccable",
    "voice": "Voice",
    "embedding": "Embedding",
    "ide-settings": "IDE settings",
}

_CLI_INSTALLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "agy": install_agy,
    "claude": install_claude,
    "grok": install_grok,
    "qwen": install_qwen,
    "codex": install_codex,
    "droid": install_droid,
}
_CLI_UNINSTALLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "agy": uninstall_agy,
    "claude": uninstall_claude,
    "grok": uninstall_grok,
    "qwen": uninstall_qwen,
    "codex": uninstall_codex,
    "droid": uninstall_droid,
}
# Uninstallers whose signature still carries the worktree ``mode`` parameter.
_MODE_UNINSTALLERS: frozenset[str] = frozenset({"qwen", "droid"})


@dataclass(frozen=True)
class EmbeddingOverrides:
    """Explicit ``--embedding-*`` values for the embedding component."""

    url: str | None = None
    provider: str | None = None
    model: str | None = None
    dim: int | None = None

    @property
    def any_set(self) -> bool:
        return any(value is not None for value in (self.url, self.provider, self.model, self.dim))


def require_installed() -> None:
    """Component runs need an existing install (bootstrap plus managed gdaemon)."""
    gdaemon = get_gobby_home() / "bin" / "gdaemon"
    if not bootstrap_path().exists() or not gdaemon.exists():
        raise click.UsageError("Gobby is not installed; run `gobby install` first.")


def reconcile_rtk_step(
    db: HubDatabase,
    rtk_flag: bool | None,
    *,
    no_interactive: bool,
) -> RtkInstallStatus:
    """Reconcile RTK for the install and report its status on stdout."""
    try:
        rtk_status = reconcile_rtk(
            db,
            rtk_flag,
            no_interactive=no_interactive,
            confirm=click.confirm,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if rtk_flag is not None or not no_interactive:
            raise click.ClickException(f"RTK reconciliation failed: {exc}") from exc
        logger.warning("RTK state unavailable during noninteractive install: %s", exc)
        rtk_status = RtkInstallStatus(
            binary_path=None,
            version=None,
            rule_enabled=False,
            direct_artifact_conflicts=(),
            health="disabled",
            managed_binary=False,
        )
    click.echo(
        "RTK: "
        f"{rtk_status.health}; rule="
        f"{'enabled' if rtk_status.rule_enabled else 'disabled'}; "
        f"binary={rtk_status.binary_path or 'unavailable'}; "
        f"version={rtk_status.version or 'unknown'}"
    )
    for conflict in rtk_status.direct_artifact_conflicts:
        click.echo(f"Warning: {conflict}", err=True)
    return rtk_status


def run_install_components(
    components: Iterable[str],
    *,
    project_path: Path,
    no_interactive: bool,
    embedding: EmbeddingOverrides | None,
    runtime: CliRuntime,
) -> dict[str, dict[str, Any]]:
    """Install the named components, in order, against an existing install."""
    results: dict[str, dict[str, Any]] = {}
    overrides = embedding or EmbeddingOverrides()
    for name in components:
        if name in CLI_COMPONENTS:
            _run_standard_cli_install(
                name,
                _CLI_INSTALLERS[name],
                project_path,
                results,
                hook_timeout_seconds=runtime.require_config().hooks.provider_timeout,
            )
        elif name == "git-hooks":
            if not (project_path / ".git").exists():
                raise click.UsageError(f"{project_path} is not a git repository")
            _run_git_hooks_install(install_git_hooks, project_path, results)
        elif name == "rtk":
            status = reconcile_rtk_step(
                runtime.require_database(), True, no_interactive=no_interactive
            )
            results["rtk"] = {
                "success": True,
                "health": status.health,
                "rule_enabled": status.rule_enabled,
                "binary_path": str(status.binary_path) if status.binary_path else None,
            }
        elif name == "impeccable":
            impeccable = provision_impeccable(project_path)
            results["impeccable"] = {
                "success": True,
                "path": str(impeccable.path),
                "version": impeccable.version,
            }
        elif name == "voice":
            _run_voice_install(
                results,
                voice_flag=True,
                no_interactive=no_interactive,
                db=runtime.require_database(),
            )
        elif name == "embedding":
            _run_embedding_install(
                install_embedding,
                results,
                no_interactive=no_interactive,
                api_base_override=overrides.url,
                model_override=overrides.model,
                dim_override=overrides.dim,
                provider_override=overrides.provider,
            )
        elif name == "ide-settings":
            configure_ide_terminals()
            results["ide-settings"] = {"success": True}
        else:
            raise click.UsageError(f"Unknown component: {name}")
    return results


def _echo_cleanup(removed: Iterable[Path], warnings: Iterable[str]) -> None:
    for path in removed:
        click.echo(f"Removed managed artifact: {path}")
    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)


def run_uninstall_components(
    components: Iterable[str],
    *,
    project_path: Path,
    runtime: CliRuntime,
) -> dict[str, dict[str, Any]]:
    """Uninstall the named components, in order. Never touches Docker or data."""
    results: dict[str, dict[str, Any]] = {}
    for name in components:
        if name in CLI_COMPONENTS:
            kwargs: dict[str, Any] = {"mode": "global"} if name in _MODE_UNINSTALLERS else {}
            _run_standard_cli_uninstall(
                name, _CLI_UNINSTALLERS[name], Path.home(), results, **kwargs
            )
        elif name == "git-hooks":
            result = uninstall_git_hooks(project_path)
            results["git-hooks"] = result
            if result["success"]:
                for hook in result.get("removed", ()):
                    click.echo(f"Removed git hook section: {hook}")
            else:
                click.echo(f"Failed: {result['error']}", err=True)
        elif name == "rtk":
            # Binary cleanup must not depend on the hub being reachable: an
            # uninstall after `gobby stop --docker` has no database to talk to.
            try:
                rule_disabled = disable_rule_if_present(runtime.require_database())
            except (
                BootstrapConfigError,
                RuntimeError,
                psycopg.OperationalError,
                PoolTimeout,
            ) as exc:
                click.echo(f"Warning: RTK rule left unchanged (hub unavailable): {exc}", err=True)
                rule_disabled = False
            cleanup = remove_managed_rtk()
            _echo_cleanup(cleanup.removed, cleanup.conflicts)
            results["rtk"] = {
                "success": True,
                "rule_disabled": rule_disabled,
                "removed": [str(path) for path in cleanup.removed],
                "conflicts": list(cleanup.conflicts),
            }
        elif name == "impeccable":
            removal = remove_impeccable_runtime()
            _echo_cleanup(removal.removed, removal.skipped)
            results["impeccable"] = {
                "success": True,
                "removed": [str(path) for path in removal.removed],
                "skipped": list(removal.skipped),
            }
        else:
            raise click.UsageError(f"Component cannot be uninstalled: {name}")
    return results
