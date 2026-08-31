"""
Installation commands for hooks.
"""

import logging
import secrets
import subprocess  # nosec B404 # fixed install startup command
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import click

from gobby.cli.install_files_home import (
    acquire_install_maintenance,
    local_install_requires_maintenance,
    peek_install_bootstrap,
    publish_install_files_home,
    resolve_install_files_home,
)
from gobby.config.bootstrap import BootstrapConfigError, DatastoreMode, load_bootstrap
from gobby.storage.auth import AuthStore, ensure_local_api_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.projects import ensure_personal_project_identity
from gobby.storage.secrets import SecretStore, write_private_file
from gobby.ui_exposure import (
    UiExposeError,
    UiExposeResult,
    apply_installer_ui_exposure,
    resolve_installer_ui_exposure,
)
from gobby.utils.local_token import local_token_path, read_local_api_token

from ._detectors import (
    _is_agy_cli_installed,
    _is_claude_code_installed,
    _is_codex_cli_installed,
    _is_droid_cli_installed,
    _is_grok_cli_installed,
    _is_qwen_cli_installed,
)
from ._install_daemon import (
    _ci_environment,
    _daemon_already_running,
    _daemon_url,
    _headless_or_remote,
    _is_source_checkout_install,
    _run_install_preflight,
)
from ._install_daemon import (
    maybe_start_daemon_after_install as _daemon_maybe_start_daemon_after_install,
)
from ._install_project import (
    _initialize_project_after_setup,
    _should_initialize_project,
)
from ._install_prompts import (
    _echo_install_details,
    _echo_install_summary,
    _run_embedding_install,
    _run_falkordb_install,
    _run_git_hooks_install,
    _run_qdrant_install,
    _run_standard_cli_install,
    _run_voice_install,
)
from ._install_state import prepare_install_state, should_configure_section
from .install_components import (
    COMPONENT_LABELS,
    COMPONENTS,
    EmbeddingOverrides,
    reconcile_rtk_step,
    require_installed,
    run_install_components,
)
from .install_identity import ensure_install_identity
from .install_setup import ensure_daemon_config, run_daemon_setup
from .install_setup_gdaemon import GdaemonInstallError, ensure_gdaemon
from .installers import (
    install_agy,
    install_claude,
    install_codex,
    install_droid,
    install_embedding,
    install_falkordb,
    install_git_hooks,
    install_grok,
    install_postgres,
    install_qdrant,
    install_qwen,
)
from .installers.container_restart import apply_managed_service_restart_policy
from .runtime import get_cli_runtime
from .utils import get_install_dir

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility (tests import from here)
_ensure_daemon_config = ensure_daemon_config

# Re-exports from extracted modules (tests import these from gobby.cli.install)
__all__ = [
    "_is_claude_code_installed",
    "_is_codex_cli_installed",
    "_is_droid_cli_installed",
    "_is_grok_cli_installed",
    "_is_qwen_cli_installed",
    "_is_agy_cli_installed",
    "_echo_install_details",
    "_ensure_daemon_config",
    "install",
]


def _maybe_start_daemon_after_install(*, no_interactive: bool, claim: Any | None = None) -> None:
    _daemon_maybe_start_daemon_after_install(
        no_interactive=no_interactive,
        daemon_url=_daemon_url,
        daemon_already_running=_daemon_already_running,
        ci_environment=_ci_environment,
        headless_or_remote=_headless_or_remote,
        claim=claim,
        subprocess_popen=subprocess.Popen,
        browser_open=webbrowser.open,
    )


def _provision_local_api_token(auth_store: AuthStore | None) -> None:
    """Provision the local token with or without a reachable hub database."""
    if auth_store is not None:
        ensure_local_api_token(auth_store)
        return
    if read_local_api_token() is not None:
        return
    token = secrets.token_urlsafe(32)
    write_private_file(local_token_path(), token.encode("utf-8"))


def _provision_gdaemon_for_services() -> None:
    """Ensure database-backed service installers use the current schema binary."""
    try:
        ensure_gdaemon()
    except (GdaemonInstallError, OSError, ValueError) as exc:
        raise click.ClickException(f"Failed to provision gdaemon: {exc}") from exc


def _resolve_ide_settings_consent(*, no_interactive: bool) -> bool:
    """Ask for consent to change detected VS Code-family terminal settings."""
    if no_interactive:
        return False

    from .installers.ide_config import find_vscode_family_ides_needing_terminal_integration

    detected_ides = find_vscode_family_ides_needing_terminal_integration()
    if not detected_ides:
        return False
    click.echo(
        f"Detected VS Code-family IDEs needing terminal integration: {', '.join(detected_ides)}"
    )
    return click.confirm(
        "Configure detected VS Code-family IDE terminals to use tmux and Gobby session titles?",
        default=True,
    )


def _install_required_stack(
    results: dict[str, dict[str, Any]],
    *,
    container_restarts: bool,
) -> None:
    """Provision PostgreSQL, Qdrant, and FalkorDB as one required stack."""
    results["postgres"] = install_postgres()
    if results["postgres"].get("success"):
        _run_qdrant_install(install_qdrant, results)
        _run_falkordb_install(install_falkordb, results)
    else:
        error = "Skipped because required PostgreSQL installation failed."
        results["qdrant"] = {"success": False, "error": error}
        results["falkordb"] = {"success": False, "error": error}

    required_services_succeeded = all(
        results[name].get("success", False) for name in ("postgres", "qdrant", "falkordb")
    )
    if required_services_succeeded:
        results["container-restarts"] = apply_managed_service_restart_policy(
            enabled=container_restarts,
        )
    else:
        results["container-restarts"] = {
            "success": False,
            "error": "Skipped because required infrastructure installation failed.",
        }


def _install_components(
    components: tuple[str, ...],
    *,
    project_path: Path,
    no_interactive: bool,
    embedding: EmbeddingOverrides,
    files_home: Path | None,
) -> None:
    """Run only the named components against an existing install."""
    if files_home is not None:
        raise click.UsageError("--files-home applies to the full install only.")
    if embedding.any_set and "embedding" not in components:
        raise click.UsageError("--embedding-* requires the embedding component.")
    require_installed()
    runtime = get_cli_runtime()
    try:
        results = run_install_components(
            components,
            project_path=project_path,
            no_interactive=no_interactive,
            embedding=embedding,
            runtime=runtime,
        )
    finally:
        runtime.close()
    failed = {name for name, result in results.items() if not result.get("success", False)}
    for name in components:
        if name not in failed:
            click.echo(f"{COMPONENT_LABELS[name]} component complete.")
    if failed:
        sys.exit(1)


@click.command("install")
@click.argument("components", nargs=-1, type=click.Choice(COMPONENTS), metavar="[COMPONENT]...")
@click.option(
    "--embedding-url",
    "embedding_url",
    default=None,
    help="Override the embedding provider's API base URL (e.g. for LM Studio on a LAN IP)",
)
@click.option(
    "--embedding-provider",
    "embedding_provider",
    type=click.Choice(["lmstudio", "ollama", "openai-compatible", "vllm"]),
    default=None,
    help=(
        "Compatibility mode for --embedding-url: lmstudio uses LM Studio-compatible "
        "defaults, ollama uses Ollama-compatible defaults, openai-compatible uses "
        "generic OpenAI-compatible embedding APIs, vllm resolves the served model "
        "from the server's /v1/models catalog."
    ),
)
@click.option(
    "--embedding-model",
    "embedding_model",
    default=None,
    help="Override the embedding model id (e.g. text-embedding-qwen3-embedding-4b)",
)
@click.option(
    "--embedding-dim",
    "embedding_dim",
    type=click.IntRange(min=1),
    default=None,
    help="Override the embedding dimension. Omit to auto-detect via /v1/embeddings probe.",
)
@click.option(
    "--no-interactive",
    "no_interactive_flag",
    is_flag=True,
    help="Skip interactive prompts (for CI/automation)",
)
@click.option(
    "--container-restarts/--no-container-restarts",
    "container_restarts_flag",
    default=True,
    help="Apply unless-stopped restart policy to managed service containers (default: enabled).",
)
@click.option(
    "--files-home",
    "files_home",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Existing absolute directory for hub-owned files (local install).",
)
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
def install(
    components: tuple[str, ...],
    embedding_url: str | None,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_dim: int | None,
    no_interactive_flag: bool,
    container_restarts_flag: bool,
    files_home: Path | None = None,
    working_dir: Path | None = None,
) -> None:
    """Install Gobby, or reinstall the named COMPONENTS of an existing install.

    Bare `gobby install` runs the full install: daemon config, the managed
    PostgreSQL/Qdrant/FalkorDB stack, account identity, hooks for every
    detected CLI, Git hooks for the current repository, and the optional
    sections. `gobby install COMPONENT...` requires an existing install and
    runs only those components, in order.

    Components: claude, codex, grok, qwen, droid, agy, git-hooks, rtk,
    impeccable, voice, embedding, ide-settings.
    """
    if embedding_provider and not embedding_url:
        raise click.UsageError("--embedding-provider requires --embedding-url.")

    embedding = EmbeddingOverrides(
        url=embedding_url,
        provider=embedding_provider,
        model=embedding_model,
        dim=embedding_dim,
    )
    project_path = working_dir.resolve() if working_dir else Path.cwd()

    if components:
        _install_components(
            tuple(dict.fromkeys(components)),
            project_path=project_path,
            no_interactive=no_interactive_flag,
            embedding=embedding,
            files_home=files_home,
        )
        return

    # Auto-detect installed CLIs
    clis_to_install: list[str] = []
    if _is_claude_code_installed():
        clis_to_install.append("claude")
    if _is_grok_cli_installed():
        clis_to_install.append("grok")
    if _is_qwen_cli_installed():
        clis_to_install.append("qwen")
    if _is_agy_cli_installed():
        clis_to_install.append("agy")
    if _is_codex_cli_installed():
        clis_to_install.append("codex")
    if _is_droid_cli_installed():
        clis_to_install.append("droid")

    install_hooks = clis_to_install != ["agy"] and (project_path / ".git").exists()
    no_supported_cli = not clis_to_install and not install_hooks

    raw_bootstrap = peek_install_bootstrap()
    datastore_mode = str(raw_bootstrap.get("datastore_mode") or "local")
    expose_ui = resolve_installer_ui_exposure(
        None,
        full_install=True,
        no_interactive=no_interactive_flag,
        confirm=lambda: click.confirm(
            "Expose the web UI to your Tailscale network?",
            default=False,
        ),
    )
    provision_managed_services = datastore_mode == "local"

    # Get install directory info
    install_dir = get_install_dir()
    is_dev_mode = _is_source_checkout_install(install_dir)

    preflight_errors, preflight_warnings = _run_install_preflight(
        is_full_install=True,
        install_dir=install_dir,
        embedding_url=embedding_url,
        embedding_provider=embedding_provider,
        managed_services=provision_managed_services,
        datastore_mode=cast(DatastoreMode, datastore_mode),
        database_url=(
            str(raw_bootstrap["database_url"]) if raw_bootstrap.get("database_url") else None
        ),
        hub_daemon_url=(
            str(raw_bootstrap["hub_daemon_url"]) if raw_bootstrap.get("hub_daemon_url") else None
        ),
    )
    if no_supported_cli:
        click.echo("No supported AI coding CLIs detected; CLI hooks will be skipped.")
    for warning in preflight_warnings:
        click.echo(f"Warning: {warning}")
    if preflight_errors:
        for error in preflight_errors:
            click.echo(f"Error: {error}", err=True)
        sys.exit(1)

    resolved_files_home = resolve_install_files_home(
        files_home,
        datastore_mode=datastore_mode,
        existing_files_home=(
            str(raw_bootstrap["files_home"]) if raw_bootstrap.get("files_home") else None
        ),
        no_interactive=no_interactive_flag,
    )
    install_claim = None
    if datastore_mode == "local" and resolved_files_home is None:
        raise click.UsageError(
            "Local install requires --files-home naming an existing absolute directory"
        )
    if local_install_requires_maintenance(datastore_mode=datastore_mode, full_install=True):
        if resolved_files_home is None:
            raise click.UsageError(
                "Local install requires --files-home naming an existing absolute directory"
            )
        install_claim = acquire_install_maintenance()
        try:
            publish_install_files_home(resolved_files_home)
        except Exception:
            install_claim.release()
            install_claim = None
            raise
        try:
            personal_marker = ensure_personal_project_identity()
        except (OSError, RuntimeError, ValueError) as exc:
            install_claim.release()
            install_claim = None
            raise click.ClickException(
                f"Failed to establish personal project identity: {exc}"
            ) from exc
        click.echo(f"Personal project identity: {personal_marker}")

    initialize_project_after_setup = _should_initialize_project(
        project_path,
        no_interactive=no_interactive_flag,
    )

    click.echo("=" * 60)
    click.echo("  Gobby Installation")
    click.echo("=" * 60)
    click.echo("\nScope: Global (~/.gobby/)")
    if is_dev_mode:
        click.echo("Mode: Development (using source directory)")

    # Track results before provisioning the required stack.
    results: dict[str, dict[str, Any]] = {}

    # Phase 1: managed PostgreSQL, daemon config, bundled content, MCP servers, IDE config
    config_result = _ensure_daemon_config()
    if config_result["created"]:
        click.echo(f"Created daemon config: {config_result['path']}")
    if provision_managed_services:
        _provision_gdaemon_for_services()
        _install_required_stack(results, container_restarts=container_restarts_flag)
        if not all(result.get("success", False) for result in results.values()):
            _echo_install_summary(results, True)
            sys.exit(1)
    configure_ide_settings = _resolve_ide_settings_consent(no_interactive=no_interactive_flag)
    run_daemon_setup(project_path, configure_ide_settings=configure_ide_settings)
    runtime = get_cli_runtime()
    try:
        try:
            db = runtime.require_database()
            installed_user = ensure_install_identity(
                db,
                no_interactive=no_interactive_flag,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException(f"Failed to establish account identity: {exc}") from exc
        click.echo(f"Account identity: {installed_user.email}")
        rtk_status = reconcile_rtk_step(db, None, no_interactive=no_interactive_flag)
        results["rtk"] = {
            "success": rtk_status.health != "unavailable",
            "path": str(rtk_status.binary_path) if rtk_status.binary_path else None,
            "version": rtk_status.version,
            "rule_enabled": rtk_status.rule_enabled,
            "health": rtk_status.health,
            "conflicts": list(rtk_status.direct_artifact_conflicts),
        }
        if initialize_project_after_setup:
            _initialize_project_after_setup(project_path)
        exposure_result: UiExposeResult | None = None
        try:
            exposure_result = apply_installer_ui_exposure(
                expose_ui,
                load_bootstrap().daemon_port,
            )
        except UiExposeError as exc:
            click.echo(
                f"Warning: failed to expose the web UI: {exc}. "
                "Install continues; run 'gobby ui expose' to retry.",
                err=True,
            )
        if exposure_result is not None:
            click.echo(f"Web UI exposed at {exposure_result.url}")

        toggles = list(clis_to_install)
        if provision_managed_services:
            toggles.extend(["postgres", "qdrant", "falkordb"])
        if install_hooks:
            toggles.append("git-hooks")

        click.echo(f"Components to configure: {', '.join(toggles)}")
        click.echo("")

        secret_store: SecretStore | None = None
        config_store: ConfigStore | None = None
        auth_store: AuthStore | None = None
        provider_hook_timeout_seconds = 120
        try:
            secret_store = SecretStore(db)
            config_store = ConfigStore(db)
            auth_store = AuthStore(db)
            provider_hook_timeout_seconds = runtime.require_config().hooks.provider_timeout
        except (
            BootstrapConfigError,
            FileNotFoundError,
            PermissionError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            # Missing config file, unavailable hub, malformed config values.
            # The orchestration proceeds with db/secret_store=None — downstream
            # steps open their own DB via _ensure_db_and_secrets if they need it.
            logger.warning(
                "Failed to initialize install database/secret store",
                extra={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                exc_info=True,
            )

        if datastore_mode == "local":
            _provision_local_api_token(auth_store)

        install_state = prepare_install_state(config_store, secret_store)

        _standard_installers: dict[str, Callable[..., dict[str, Any]]] = {
            "agy": install_agy,
            "claude": install_claude,
            "grok": install_grok,
            "qwen": install_qwen,
            "codex": install_codex,
            "droid": install_droid,
        }
        for cli_name, installer_fn in _standard_installers.items():
            if cli_name in clis_to_install:
                _run_standard_cli_install(
                    cli_name,
                    installer_fn,
                    project_path,
                    results,
                    hook_timeout_seconds=provider_hook_timeout_seconds,
                )

        if install_hooks:
            _run_git_hooks_install(install_git_hooks, project_path, results)

        configure_embedding = should_configure_section(
            install_state.embedding,
            label="embedding provider/model/endpoint",
            no_interactive=no_interactive_flag,
            explicit=embedding.any_set,
        )
        if configure_embedding:
            _run_embedding_install(
                install_embedding,
                results,
                no_interactive=no_interactive_flag,
                api_base_override=embedding.url,
                model_override=embedding.model,
                dim_override=embedding.dim,
                provider_override=embedding.provider,
            )

        configure_voice = should_configure_section(
            install_state.voice,
            label="voice setting",
            no_interactive=no_interactive_flag,
            explicit=False,
        )
        if configure_voice:
            _run_voice_install(
                results,
                voice_flag=False,
                no_interactive=no_interactive_flag,
                db=db,
                secret_store=secret_store,
                reconfigure=install_state.voice.configured,
                current_enabled=install_state.voice.enabled,
            )

        # Summary, next steps, API key prompts
        all_success = _echo_install_summary(
            results,
            no_interactive_flag,
            db=db,
            secret_store=secret_store,
        )
        if not all_success:
            sys.exit(1)
        _maybe_start_daemon_after_install(
            no_interactive=no_interactive_flag,
            claim=install_claim,
        )
    finally:
        if install_claim is not None:
            install_claim.release()
        runtime.close()
