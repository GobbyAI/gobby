"""
Installation commands for hooks.
"""

import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess  # nosec B404 # fixed install preflight/start commands
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from gobby.config.bootstrap import (
    DEFAULT_DAEMON_PORT,
    DEFAULT_WEBSOCKET_PORT,
    BootstrapConfigError,
)
from gobby.config.bootstrap_io import update_bootstrap_yaml
from gobby.storage.auth import ensure_local_api_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
    SecretStore,
    write_private_file,
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
)
from ._install_daemon import (
    maybe_start_daemon_after_install as _daemon_maybe_start_daemon_after_install,
)
from ._install_legacy import _raise_graph_backend_removed
from ._install_project import (
    _initialize_project_after_setup,
    _should_initialize_project,
)
from ._install_prompts import (
    _API_KEY_PROMPTS,
    _echo_install_details,
    _echo_install_summary,
    _echo_migration_notice,
    _echo_uninstall_details,
    _echo_uninstall_summary,
    _prompt_api_keys,
    _run_embedding_install,
    _run_falkordb_install,
    _run_git_hooks_install,
    _run_qdrant_install,
    _run_standard_cli_install,
    _run_standard_cli_uninstall,
    _run_voice_install,
)
from ._install_state import empty_install_state, prepare_install_state, should_configure_section
from .install_setup import ensure_daemon_config, run_daemon_setup
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
    uninstall_agy,
    uninstall_claude,
    uninstall_codex,
    uninstall_droid,
    uninstall_grok,
    uninstall_qwen,
)
from .utils import get_install_dir, load_full_config_from_db

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
    "_echo_uninstall_details",
    "_API_KEY_PROMPTS",
    "_prompt_api_keys",
    "_ensure_daemon_config",
    "install",
    "uninstall",
]


def _is_source_checkout_install(install_dir: Path) -> bool:
    resolved = install_dir.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        install_package = candidate / "src" / "gobby" / "install"
        if install_package.is_dir() and (
            (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists()
        ):
            return True
    return False


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(  # nosec B603 B607
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _port_available(port: int, host: str = "0.0.0.0") -> bool:  # nosec B104
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _run_install_preflight(
    *,
    is_full_install: bool,
    detected_clis: list[str],
    install_dir: Path,
    embedding_url: str | None,
    embedding_provider: str | None,
) -> tuple[list[str], list[str]]:
    """Return full-install preflight errors and optional warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    if is_full_install:
        if not _docker_daemon_available():
            errors.append("Docker daemon is required for full install. Start Docker and retry.")
        if not detected_clis:
            errors.append(
                "At least one supported coding CLI is required for full install "
                "(Claude Code, AGY, Codex, Grok, Qwen, or Droid)."
            )
        python_version = tuple(int(part) for part in platform.python_version_tuple()[:2])
        if python_version < (3, 13):
            current = platform.python_version()
            errors.append(f"Python >= 3.13 is required; current Python is {current}.")
        if os.name == "posix" and shutil.which("tmux") is None:
            errors.append("tmux is required on POSIX systems. Install tmux and retry.")
        if _is_source_checkout_install(install_dir) and shutil.which("uv") is None:
            errors.append("uv is required when installing from a source checkout.")

        if not embedding_url and not embedding_provider:
            warnings.append(
                "No embedding provider override supplied; install will prompt or keep "
                "semantic features disabled."
            )

    if shutil.which("git") is None:
        warnings.append(
            "git was not found on PATH; project initialization and hooks may be limited."
        )

    for port in (DEFAULT_DAEMON_PORT, DEFAULT_WEBSOCKET_PORT):
        if not _port_available(port):
            warnings.append(f"Port {port} is already in use.")

    return errors, warnings


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


def _provision_local_api_token(config_store: ConfigStore | None) -> None:
    """Provision the local token with or without a reachable hub database."""
    if config_store is not None:
        ensure_local_api_token(config_store)
        return
    if read_local_api_token() is not None:
        return
    token = secrets.token_urlsafe(32)
    write_private_file(local_token_path(), token.encode("utf-8"))


def _set_bootstrap_auth_mode(path: Path, auth_mode: str) -> None:
    def apply_auth_mode(bootstrap: dict[str, Any]) -> None:
        bootstrap["auth_mode"] = auth_mode

    update_bootstrap_yaml(path, apply_auth_mode)


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
    help="Install hooks for all detected CLIs (default behavior when no flags specified)",
)
@click.option(
    "--config-only",
    "config_only_flag",
    is_flag=True,
    help="Initialize daemon configuration and database without installing hooks or services",
)
@click.option(
    "--falkordb",
    "falkordb_flag",
    is_flag=True,
    default=False,
    help="Install only the FalkorDB service",
)
@click.option(
    "--falkordb-password-stdin",
    "falkordb_password_stdin",
    is_flag=True,
    help="Read a custom FalkorDB password from stdin",
)
@click.option(
    "--neo4j-password",
    "deprecated_neo4j_password",
    default=None,
    hidden=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: _raise_graph_backend_removed()
    if value is not None
    else None,
)
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: _raise_graph_backend_removed() if value else None,
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
    "--auth-mode",
    type=click.Choice(["required", "disabled"]),
    default=None,
    help="Daemon API authentication mode to persist in bootstrap.yaml.",
)
@click.option(
    "--ide-settings/--no-ide-settings",
    "ide_settings_flag",
    default=None,
    help="Configure detected VS Code-family terminals for tmux and Gobby session titles.",
)
@click.option(
    "--no-interactive",
    "no_interactive_flag",
    is_flag=True,
    help="Skip interactive prompts (for CI/automation)",
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
    falkordb_flag: bool,
    falkordb_password_stdin: bool,
    voice_flag: bool,
    project_flag: bool,
    embedding_url: str | None,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_dim: int | None,
    secret_kek_posture: str,
    auth_mode: str | None,
    ide_settings_flag: bool | None,
    no_interactive_flag: bool,
    working_dir: Path | None,
) -> None:
    """Install Gobby hooks to AI coding CLIs and Git.

    By default (no flags), installs hooks globally (one-time setup).
    Use --project to install per-project instead (legacy behavior).
    Use --claude, --grok, --agy, --qwen, --codex, or --droid to install only
    to specific CLIs.
    Use --hooks to install Git hooks for verification, JSONL export, and code indexing.
    Use --config-only to initialize daemon configuration and database only.
    """
    if embedding_provider and not embedding_url:
        raise click.UsageError("--embedding-provider requires --embedding-url.")

    falkordb_password: str | None = None
    if falkordb_password_stdin:
        falkordb_password = sys.stdin.read().strip()
        if not falkordb_password:
            raise click.UsageError("--falkordb-password-stdin requires a password on stdin.")

    if falkordb_flag:
        service_results: dict[str, dict[str, Any]] = {}
        _run_falkordb_install(install_falkordb, falkordb_password, service_results)
        if not _echo_install_summary(service_results, no_interactive_flag):
            sys.exit(1)
        return

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
        and not falkordb_flag
        and not all_flag
        and not config_only_flag
    ):
        all_flag = True
    is_full_install = all_flag

    clis_to_install: list[str] = []

    install_hooks = hooks_flag

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
            click.echo("No supported AI coding CLIs detected.")
            click.echo("\nSupported CLIs:")
            click.echo("  - Claude Code: npm install -g @anthropic-ai/claude-code")
            click.echo("  - Grok CLI:    install the Grok CLI")
            click.echo("  - Qwen CLI:    npm install -g @qwen-code/qwen-code")
            click.echo("  - Codex CLI:   npm install -g @openai/codex")
            click.echo("  - Droid CLI:   curl -fsSL https://app.factory.ai/cli | sh")
            click.echo("  - AGY CLI:     install Google Antigravity CLI")
            click.echo(
                "\nYou can still install manually with --claude, --grok, --qwen, "
                "--agy, --codex, or --droid flags."
            )
            sys.exit(1)
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

    # Get install directory info
    install_dir = get_install_dir()
    is_dev_mode = _is_source_checkout_install(install_dir)

    preflight_errors, preflight_warnings = _run_install_preflight(
        is_full_install=is_full_install,
        detected_clis=clis_to_install,
        install_dir=install_dir,
        embedding_url=embedding_url,
        embedding_provider=embedding_provider,
    )
    for warning in preflight_warnings:
        click.echo(f"Warning: {warning}")
    if preflight_errors:
        for error in preflight_errors:
            click.echo(f"Error: {error}", err=True)
        sys.exit(1)

    initialize_project_after_setup = not config_only_flag and _should_initialize_project(
        project_path,
        no_interactive=no_interactive_flag,
    )

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Installation")
    click.echo("=" * 60)
    if mode == "global":
        click.echo("\nScope: Global (hooks installed to ~/.gobby/hooks/)")
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
    if auth_mode is not None:
        _set_bootstrap_auth_mode(Path(config_result["path"]), auth_mode)
        click.echo(f"Daemon API authentication mode: {auth_mode}")
    if is_full_install:
        postgres_result = install_postgres()
        results["postgres"] = postgres_result
        if not postgres_result.get("success"):
            click.echo(
                f"Error: PostgreSQL installation failed: {postgres_result.get('error', 'unknown error')}",
                err=True,
            )
            sys.exit(1)
    configure_ide_settings = False
    if not config_only_flag:
        configure_ide_settings = _resolve_ide_settings_consent(
            ide_settings_flag,
            no_interactive=no_interactive_flag,
        )
    run_daemon_setup(project_path, configure_ide_settings=configure_ide_settings)
    if initialize_project_after_setup:
        _initialize_project_after_setup(project_path)
    if config_only_flag:
        click.echo("Configuration and database initialization complete.")
        return

    toggles = list(clis_to_install)
    if is_full_install:
        toggles.extend(["postgres", "qdrant", "falkordb"])
    if install_hooks:
        toggles.append("git-hooks")

    click.echo(f"Components to configure: {', '.join(toggles)}")
    click.echo("")

    db: HubDatabase | None = None
    secret_store: SecretStore | None = None
    config_store: ConfigStore | None = None

    try:
        from gobby.storage.hub.runtime import open_runtime_hub_database

        load_full_config_from_db()
        db = open_runtime_hub_database()
        secret_store = SecretStore(db)
        config_store = ConfigStore(db)
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
            "Failed to initialize install database/secret store (%s): %s",
            type(exc).__name__,
            exc,
        )

    _configure_secret_kek_posture(
        secret_store,
        secret_kek_posture,
        no_interactive=no_interactive_flag,
    )
    _provision_local_api_token(config_store)

    try:
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
                _run_standard_cli_install(cli_name, installer_fn, project_path, mode, results)

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

        if is_full_install:
            _run_qdrant_install(install_qdrant, results)
            _run_falkordb_install(install_falkordb, falkordb_password, results)

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
        if db is not None:
            db.close()


@click.command("uninstall")
@click.option(
    "--claude",
    "claude_flag",
    is_flag=True,
    help="Uninstall Claude Code hooks only",
)
@click.option(
    "--grok",
    "grok_flag",
    is_flag=True,
    help="Uninstall Grok CLI hooks only",
)
@click.option(
    "--codex",
    "codex_flag",
    is_flag=True,
    help="Uninstall Codex notify integration",
)
@click.option(
    "--droid",
    "droid_flag",
    is_flag=True,
    help="Uninstall Droid CLI hooks only",
)
@click.option(
    "--agy",
    "agy_flag",
    is_flag=True,
    help="Uninstall AGY CLI hooks only",
)
@click.option(
    "--qwen",
    "qwen_flag",
    is_flag=True,
    help="Uninstall Qwen CLI hooks only",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Uninstall hooks from all CLIs (default behavior when no flags specified)",
)
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: _raise_graph_backend_removed() if value else None,
)
@click.option(
    "--project",
    "project_flag",
    is_flag=True,
    help="Uninstall per-project hooks from current directory (instead of global)",
)
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(
    claude_flag: bool,
    grok_flag: bool,
    agy_flag: bool,
    codex_flag: bool,
    droid_flag: bool,
    qwen_flag: bool,
    all_flag: bool,
    project_flag: bool,
    working_dir: Path | None,
) -> None:
    """Uninstall Gobby hooks from AI coding CLIs.

    By default (no flags), uninstalls global hooks from CLI settings and ~/.gobby/hooks/.
    Use --project to uninstall per-project hooks from the current directory.
    Use --claude, --grok, --agy, --qwen, or --codex to uninstall only from
    specific CLIs.
    """
    project_path = working_dir.resolve() if working_dir else Path.cwd()

    # Determine which CLIs to uninstall
    if (
        not claude_flag
        and not grok_flag
        and not agy_flag
        and not qwen_flag
        and not codex_flag
        and not droid_flag
        and not all_flag
    ):
        all_flag = True

    # Build list of CLIs to uninstall
    clis_to_uninstall: list[str] = []

    if all_flag:
        if project_flag:
            claude_settings = project_path / ".claude" / "settings.json"
            grok_hooks = Path.home() / ".grok" / "hooks" / "gobby.json"
            agy_hooks = Path.home() / ".gemini" / "config" / "hooks.json"
            qwen_settings = project_path / ".qwen" / "settings.json"
            codex_hooks = project_path / ".codex" / "hooks.json"
            droid_hooks = project_path / ".factory" / "hooks" / "hooks.json"
        else:
            claude_settings = Path.home() / ".claude" / "settings.json"
            grok_hooks = Path.home() / ".grok" / "hooks" / "gobby.json"
            agy_hooks = Path.home() / ".gemini" / "config" / "hooks.json"
            qwen_settings = Path.home() / ".qwen" / "settings.json"
            codex_hooks = Path.home() / ".codex" / "hooks.json"
            droid_hooks = Path.home() / ".factory" / "hooks" / "hooks.json"

        if claude_settings.exists():
            clis_to_uninstall.append("claude")
        if grok_hooks.exists():
            clis_to_uninstall.append("grok")
        if agy_hooks.exists():
            clis_to_uninstall.append("agy")
        if qwen_settings.exists():
            clis_to_uninstall.append("qwen")
        if codex_hooks.exists():
            clis_to_uninstall.append("codex")
        if droid_hooks.exists():
            clis_to_uninstall.append("droid")

        if not clis_to_uninstall:
            click.echo("No Gobby hooks found to uninstall.")
            if project_flag:
                click.echo(f"\nChecked: {project_path / '.claude'}")
                click.echo(f"         {Path.home() / '.grok' / 'hooks' / 'gobby.json'}")
                click.echo(f"         {Path.home() / '.gemini' / 'config' / 'hooks.json'}")
                click.echo(f"         {project_path / '.qwen'}")
                click.echo(f"         {project_path / '.codex'}")
                click.echo(f"         {project_path / '.factory'}")
            else:
                click.echo(f"\nChecked: {Path.home() / '.claude'}")
                click.echo(f"         {Path.home() / '.grok' / 'hooks' / 'gobby.json'}")
                click.echo(f"         {Path.home() / '.gemini' / 'config' / 'hooks.json'}")
                click.echo(f"         {Path.home() / '.qwen'}")
                click.echo(f"         {Path.home() / '.codex'}")
                click.echo(f"         {Path.home() / '.factory'}")
            sys.exit(0)
    else:
        if claude_flag:
            clis_to_uninstall.append("claude")
        if grok_flag:
            clis_to_uninstall.append("grok")
        if agy_flag:
            clis_to_uninstall.append("agy")
        if qwen_flag:
            clis_to_uninstall.append("qwen")
        if codex_flag:
            clis_to_uninstall.append("codex")
        if droid_flag:
            clis_to_uninstall.append("droid")

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Uninstallation")
    click.echo("=" * 60)
    if project_flag:
        click.echo(f"\nScope: Project ({project_path})")
    else:
        click.echo("\nScope: Global")
    click.echo(f"Targets to uninstall: {', '.join(clis_to_uninstall)}")
    click.echo("")

    # For global uninstall, use Path.home() so uninstallers find ~/.{cli}/
    uninstall_base = project_path if project_flag else Path.home()

    # Track results
    results: dict[str, dict[str, Any]] = {}

    # Standard CLIs (claude, grok, agy, qwen, codex, droid)
    _standard_uninstallers: dict[str, Callable[..., dict[str, Any]]] = {
        "agy": uninstall_agy,
        "claude": uninstall_claude,
        "grok": uninstall_grok,
        "qwen": uninstall_qwen,
        "codex": uninstall_codex,
        "droid": uninstall_droid,
    }
    for cli_name, uninstaller_fn in _standard_uninstallers.items():
        if cli_name in clis_to_uninstall:
            uninstall_kwargs: dict[str, Any] = {}
            if cli_name in {"qwen", "droid"}:
                uninstall_kwargs["mode"] = "project" if project_flag else "global"
            _run_standard_cli_uninstall(
                cli_name,
                uninstaller_fn,
                uninstall_base,
                results,
                **uninstall_kwargs,
            )

    # Remove global hooks directory for global uninstall
    if not project_flag and all_flag:
        global_hooks_dir = Path(
            os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks"))
        )
        for fname in ("hook_dispatcher.py", "validate_settings.py"):
            fpath = global_hooks_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError as e:
                    click.echo(f"  Warning: could not remove {fpath}: {e}", err=True)
        click.echo("Removed global hook dispatchers from ~/.gobby/hooks/")
        click.echo("")

    # Summary
    all_success = _echo_uninstall_summary(results)
    if not all_success:
        sys.exit(1)
