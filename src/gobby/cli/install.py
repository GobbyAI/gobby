"""
Installation commands for hooks.
"""

import logging
import os
import secrets
import subprocess  # nosec B404 # fixed install startup command
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.config.persistence import validate_falkordb_password
from gobby.storage.auth import AuthStore, ensure_local_api_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.projects import ensure_personal_project_identity
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
    SecretStore,
    write_private_file,
)
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
    _API_KEY_PROMPTS,
    _echo_install_details,
    _echo_install_summary,
    _echo_migration_notice,
    _prompt_api_keys,
    _run_embedding_install,
    _run_falkordb_install,
    _run_git_hooks_install,
    _run_qdrant_install,
    _run_standard_cli_install,
    _run_voice_install,
)
from ._install_state import empty_install_state, prepare_install_state, should_configure_section
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
    "_API_KEY_PROMPTS",
    "_prompt_api_keys",
    "_ensure_daemon_config",
    "install",
]


def _maybe_start_daemon_after_install(*, no_interactive: bool) -> None:
    _daemon_maybe_start_daemon_after_install(
        no_interactive=no_interactive,
        daemon_url=_daemon_url,
        daemon_already_running=_daemon_already_running,
        ci_environment=_ci_environment,
        headless_or_remote=_headless_or_remote,
        subprocess_popen=subprocess.Popen,
        browser_open=webbrowser.open,
    )


def _configure_secret_kek_posture(
    secret_store: SecretStore | None,
    posture: str,
    *,
    no_interactive: bool,
) -> None:
    storage_posture = POSTURE_SCRYPT_PASSPHRASE if posture == "passphrase" else POSTURE_KEY_FILE
    if storage_posture == POSTURE_KEY_FILE:
        if secret_store is None:
            return
        if secret_store.current_kek_posture() == POSTURE_SCRYPT_PASSPHRASE:
            current_passphrase = os.environ.get(SECRET_KEK_PASSPHRASE_ENV)
            if not current_passphrase:
                if no_interactive:
                    raise click.ClickException(
                        f"--secret-kek-posture key-file requires current "
                        f"{SECRET_KEK_PASSPHRASE_ENV} in --no-interactive mode."
                    )
                current_passphrase = str(
                    click.prompt(
                        "Current secret KEK passphrase",
                        hide_input=True,
                    )
                )
            secret_store.kek_passphrase = current_passphrase
        secret_store.set_kek_posture(storage_posture)
        click.echo("Secret KEK posture: key-file")
        return
    if secret_store is None:
        raise click.ClickException("Cannot configure passphrase KEK posture without hub access.")

    passphrase = os.environ.get(SECRET_KEK_PASSPHRASE_ENV)
    if not passphrase:
        if no_interactive:
            raise click.ClickException(
                f"--secret-kek-posture passphrase requires {SECRET_KEK_PASSPHRASE_ENV} "
                "in --no-interactive mode."
            )
        passphrase = str(
            click.prompt(
                "Secret KEK passphrase",
                hide_input=True,
                confirmation_prompt=True,
            )
        )
    secret_store.set_kek_posture(storage_posture, passphrase=passphrase)
    click.echo("Secret KEK posture: passphrase")


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


def _resolve_ide_settings_consent(
    ide_settings: bool | None,
    *,
    no_interactive: bool,
) -> bool:
    """Resolve explicit or interactive consent for VS Code-family settings changes."""
    if ide_settings is not None:
        return ide_settings
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
    falkordb_password: str | None,
    container_restarts: bool,
) -> None:
    """Provision PostgreSQL, Qdrant, and FalkorDB as one required stack."""
    results["postgres"] = install_postgres()
    if results["postgres"].get("success"):
        _run_qdrant_install(install_qdrant, results)
        _run_falkordb_install(install_falkordb, falkordb_password, results)
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


@click.command("install")
@click.option(
    "--claude",
    "claude_flag",
    is_flag=True,
    help="Install Claude Code hooks only",
)
@click.option(
    "--grok",
    "grok_flag",
    is_flag=True,
    help="Install Grok CLI hooks only",
)
@click.option(
    "--agy",
    "agy_flag",
    is_flag=True,
    help="Install AGY CLI hooks only",
)
@click.option(
    "--codex",
    "codex_flag",
    is_flag=True,
    help="Configure Codex notify integration (interactive Codex)",
)
@click.option(
    "--droid",
    "droid_flag",
    is_flag=True,
    help="Install Droid CLI hooks only",
)
@click.option(
    "--qwen",
    "qwen_flag",
    is_flag=True,
    help="Install Qwen CLI hooks only",
)
@click.option(
    "--hooks",
    "--git-hooks",
    "hooks_flag",
    is_flag=True,
    help="Install Git hooks for verification, JSONL export, and code indexing",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Install required infrastructure and hooks for all detected CLIs",
)
@click.option(
    "--config-only",
    "config_only_flag",
    is_flag=True,
    help="Configure Gobby and required infrastructure without installing CLI or Git hooks",
)
@click.option(
    "--falkordb-password-stdin",
    "falkordb_password_stdin",
    is_flag=True,
    help="Read a custom FalkorDB password from stdin",
)
@click.option(
    "--project",
    "project_flag",
    is_flag=True,
    help="Install hooks per-project instead of globally (legacy behavior)",
)
@click.option(
    "--voice",
    "voice_flag",
    is_flag=True,
    help="Install voice chat dependencies (STT + TTS with voice cloning)",
)
@click.option(
    "--embedding-url",
    "embedding_url",
    default=None,
    help="Override the embedding provider's API base URL (e.g. for LM Studio on a LAN IP)",
)
@click.option(
    "--embedding-provider",
    "embedding_provider",
    type=click.Choice(["lmstudio", "ollama", "openai-compatible"]),
    default=None,
    help=(
        "Compatibility mode for --embedding-url: lmstudio uses LM Studio-compatible "
        "defaults, ollama uses Ollama-compatible defaults, openai-compatible uses "
        "generic OpenAI-compatible embedding APIs."
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
    "--secret-kek-posture",
    "secret_kek_posture",
    type=click.Choice(["key-file", "passphrase"]),
    default="key-file",
    show_default=True,
    help="KEK posture for daemon-local secret encryption.",
)
@click.option(
    "--ide-settings/--no-ide-settings",
    "ide_settings_flag",
    default=None,
    help="Configure detected VS Code-family terminals for tmux and Gobby session titles.",
)
@click.option(
    "--expose-ui/--no-expose-ui",
    "expose_ui_flag",
    default=None,
    help="Expose the web UI to this machine's Tailscale network.",
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
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
def install(
    claude_flag: bool,
    grok_flag: bool,
    agy_flag: bool,
    codex_flag: bool,
    droid_flag: bool,
    qwen_flag: bool,
    hooks_flag: bool,
    all_flag: bool,
    config_only_flag: bool,
    falkordb_password_stdin: bool,
    voice_flag: bool,
    project_flag: bool,
    embedding_url: str | None,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_dim: int | None,
    secret_kek_posture: str,
    ide_settings_flag: bool | None,
    expose_ui_flag: bool | None,
    no_interactive_flag: bool,
    container_restarts_flag: bool,
    working_dir: Path | None,
) -> None:
    """Install Gobby configuration, required infrastructure, and integrations.

    By default (no flags), provisions the required stack and installs hooks
    globally for detected CLIs.
    Use --project to install per-project instead (legacy behavior).
    Use --claude, --grok, --agy, --qwen, --codex, or --droid to install only
    to specific CLIs.
    Use --hooks alone to reinstall Git hooks without configuration or infrastructure setup.
    Use --config-only to configure Gobby and required infrastructure without hooks.
    """
    if embedding_provider and not embedding_url:
        raise click.UsageError("--embedding-provider requires --embedding-url.")

    falkordb_password: str | None = None
    if falkordb_password_stdin:
        falkordb_password = sys.stdin.read().strip()
        if not falkordb_password:
            raise click.UsageError("--falkordb-password-stdin requires a password on stdin.")
        try:
            validate_falkordb_password(falkordb_password)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc

    project_path = working_dir.resolve() if working_dir else Path.cwd()
    mode = "project" if project_flag else "global"
    if project_flag and agy_flag:
        raise click.UsageError(
            "AGY integration does not support --project; install AGY globally without --project."
        )

    if (
        not claude_flag
        and not grok_flag
        and not agy_flag
        and not qwen_flag
        and not codex_flag
        and not droid_flag
        and not hooks_flag
        and not all_flag
        and not config_only_flag
    ):
        all_flag = True
    clis_to_install: list[str] = []

    install_hooks = hooks_flag
    no_supported_cli = False

    if all_flag:
        # Auto-detect installed CLIs
        if _is_claude_code_installed():
            clis_to_install.append("claude")
        if _is_grok_cli_installed():
            clis_to_install.append("grok")
        if _is_qwen_cli_installed():
            clis_to_install.append("qwen")
        if not project_flag and _is_agy_cli_installed():
            clis_to_install.append("agy")
        if _is_codex_cli_installed():
            clis_to_install.append("codex")
        if _is_droid_cli_installed():
            clis_to_install.append("droid")

        # Check for git
        if clis_to_install != ["agy"] and (project_path / ".git").exists():
            install_hooks = True

        if not clis_to_install and not install_hooks:
            no_supported_cli = True

    else:
        if claude_flag:
            clis_to_install.append("claude")
        if grok_flag:
            clis_to_install.append("grok")
        if agy_flag:
            clis_to_install.append("agy")
        if qwen_flag:
            clis_to_install.append("qwen")
        if codex_flag:
            clis_to_install.append("codex")
        if droid_flag:
            clis_to_install.append("droid")

    is_full_install = all_flag or config_only_flag
    bootstrap = load_bootstrap()
    expose_ui = resolve_installer_ui_exposure(
        expose_ui_flag,
        full_install=is_full_install,
        no_interactive=no_interactive_flag,
        confirm=lambda: click.confirm(
            "Expose the web UI to your Tailscale network?",
            default=False,
        ),
    )
    datastore_mode = bootstrap.datastore_mode
    provision_managed_services = is_full_install and datastore_mode == "local"
    hooks_only_maintenance = (
        hooks_flag
        and not clis_to_install
        and not is_full_install
        and not voice_flag
        and not falkordb_password_stdin
        and not any(
            value is not None
            for value in (embedding_url, embedding_provider, embedding_model, embedding_dim)
        )
        and ide_settings_flag is None
        and expose_ui_flag is not True
    )

    # Get install directory info
    install_dir = get_install_dir()
    is_dev_mode = _is_source_checkout_install(install_dir)

    preflight_errors, preflight_warnings = _run_install_preflight(
        is_full_install=is_full_install,
        install_dir=install_dir,
        embedding_url=embedding_url,
        embedding_provider=embedding_provider,
        managed_services=provision_managed_services,
        datastore_mode=datastore_mode,
        database_url=bootstrap.database_url,
    )
    if no_supported_cli:
        click.echo("No supported AI coding CLIs detected; CLI hooks will be skipped.")
    for warning in preflight_warnings:
        click.echo(f"Warning: {warning}")
    if preflight_errors:
        for error in preflight_errors:
            click.echo(f"Error: {error}", err=True)
        sys.exit(1)

    try:
        personal_marker = ensure_personal_project_identity()
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(f"Failed to establish personal project identity: {exc}") from exc
    click.echo(f"Personal project identity: {personal_marker}")

    if hooks_only_maintenance:
        hook_results: dict[str, dict[str, Any]] = {}
        _run_git_hooks_install(install_git_hooks, project_path, hook_results)
        if not hook_results["git-hooks"].get("success", False):
            sys.exit(1)
        click.echo("Git hook maintenance complete.")
        return

    initialize_project_after_setup = not config_only_flag and _should_initialize_project(
        project_path,
        no_interactive=no_interactive_flag,
    )

    click.echo("=" * 60)
    click.echo("  Gobby Installation")
    click.echo("=" * 60)
    if mode == "global":
        click.echo("\nScope: Global (~/.gobby/)")
    else:
        click.echo(f"\nScope: Project ({project_path})")
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
        _install_required_stack(
            results,
            falkordb_password=falkordb_password,
            container_restarts=container_restarts_flag,
        )
        if not all(result.get("success", False) for result in results.values()):
            _echo_install_summary(results, True)
            sys.exit(1)
    configure_ide_settings = False
    if not config_only_flag:
        configure_ide_settings = _resolve_ide_settings_consent(
            ide_settings_flag,
            no_interactive=no_interactive_flag,
        )
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
        if initialize_project_after_setup:
            _initialize_project_after_setup(project_path)
        exposure_result: UiExposeResult | None = None
        try:
            exposure_result = apply_installer_ui_exposure(
                expose_ui,
                bootstrap.daemon_port,
            )
        except UiExposeError as exc:
            click.echo(
                f"Warning: failed to expose the web UI: {exc}. "
                "Install continues; run 'gobby ui expose' to retry.",
                err=True,
            )
        if exposure_result is not None:
            click.echo(f"Web UI exposed at {exposure_result.url}")
        if config_only_flag:
            if not _echo_install_summary(results, True):
                sys.exit(1)
            click.echo("Configuration and required infrastructure complete.")
            return

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
            _configure_secret_kek_posture(
                secret_store,
                secret_kek_posture,
                no_interactive=no_interactive_flag,
            )
            _provision_local_api_token(auth_store)

        install_state = empty_install_state()
        if is_full_install:
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
                    mode,
                    results,
                    hook_timeout_seconds=provider_hook_timeout_seconds,
                )

        if install_hooks:
            _run_git_hooks_install(install_git_hooks, project_path, results)

        embedding_override = any(
            value is not None
            for value in (embedding_url, embedding_provider, embedding_model, embedding_dim)
        )
        configure_embedding = is_full_install and should_configure_section(
            install_state.embedding,
            label="embedding provider/model/endpoint",
            no_interactive=no_interactive_flag,
            explicit=embedding_override,
        )
        if configure_embedding:
            _run_embedding_install(
                install_embedding,
                results,
                no_interactive=no_interactive_flag,
                api_base_override=embedding_url,
                model_override=embedding_model,
                dim_override=embedding_dim,
                provider_override=embedding_provider,
            )

        configure_voice = not is_full_install or should_configure_section(
            install_state.voice,
            label="voice setting",
            no_interactive=no_interactive_flag,
            explicit=voice_flag,
        )
        if configure_voice:
            _run_voice_install(
                results,
                voice_flag=voice_flag,
                no_interactive=no_interactive_flag,
                db=db,
                secret_store=secret_store,
                reconfigure=install_state.voice.configured,
                current_enabled=install_state.voice.enabled,
            )

        # Migration detection
        if mode == "global":
            _echo_migration_notice(project_path)

        # Summary, next steps, API key prompts
        all_success = _echo_install_summary(
            results,
            no_interactive_flag,
            db=db,
            secret_store=secret_store,
        )
        if not all_success:
            sys.exit(1)
        if is_full_install:
            _maybe_start_daemon_after_install(no_interactive=no_interactive_flag)
    finally:
        runtime.close()
