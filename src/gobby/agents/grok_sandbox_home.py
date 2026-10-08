"""Per-run Grok configuration isolates runtime policy-cache rewrites from the host."""

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.sandbox_control_paths import (
    GROK_CONTROL_FILES,
    GROK_RUNTIME_DIRECTORIES,
    assert_credential_read_contract,
)
from gobby.paths import get_gobby_home


@dataclass(frozen=True)
class GrokSandboxHome:
    home: Path
    protected_write_paths: tuple[str, ...]
    runtime_write_paths: tuple[str, ...]


def prepare_grok_sandbox_home(
    cache: Path, env: Mapping[str, str], *, assets: Path
) -> GrokSandboxHome:
    """Fork control files, link authentication without copying it, share runtime state.

    Only daemon-owned assets record the original host home. A resumed managed
    home therefore cannot choose a new host source through its writable files.
    """
    from gobby.agents.sandbox_policy import (
        assert_sensitive_path_contract,
        credential_read_roots,
        provider_credential_read_exceptions,
    )

    source = Path(env.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser()
    if source.is_symlink() or cache.is_symlink():
        raise ValueError("Grok sandbox home must use real directories")
    source = source.resolve()
    old_receipt = source.parent.parent / "assets/grok-home-source.json"
    run_root = (get_gobby_home() / "run/sandbox").resolve()
    if source.name == "grok-home" and source.is_relative_to(run_root):
        if old_receipt.is_symlink():
            raise ValueError("Grok source receipt must not be a symlink")
        receipt = json.loads(old_receipt.read_text())
        if not isinstance(receipt, dict) or not isinstance(receipt.get("source"), str):
            raise ValueError("invalid Grok source receipt")
        source = Path(receipt["source"]).resolve()
    assert_sensitive_path_contract([str(source)])
    assert_credential_read_contract(
        [str(source)], credential_read_roots(), provider_credential_read_exceptions("grok")
    )
    auth = Path(env.get("GROK_AUTH_PATH") or str(source / "auth.json")).expanduser().resolve()
    auth_lock = auth.with_name("auth.json.lock")
    auth_writes = (str(auth), str(auth_lock), str(auth.parent / ".*.*.tmp"))
    assert_sensitive_path_contract(list(auth_writes), list(auth_writes))
    assert_credential_read_contract(
        [str(auth), str(auth_lock.resolve())],
        credential_read_roots(),
        provider_credential_read_exceptions("grok"),
    )
    if auth_lock.is_symlink():
        raise ValueError("Grok auth lock must not be a symlink")

    protected = [str(source / name) for name in GROK_CONTROL_FILES]
    registry = source / "hooks-paths"
    if registry.is_symlink():
        raise ValueError("Grok hook registry must not be a symlink")
    if registry.is_file():
        protected.extend(
            line.strip()
            for line in registry.read_text().splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and Path(line.strip()).is_absolute()
        )
    runtime = (*auth_writes, *(str(source / name) for name in GROK_RUNTIME_DIRECTORIES))
    resolved_runtime = [str(Path(path).resolve()) for path in runtime]
    assert_sensitive_path_contract(resolved_runtime, resolved_runtime)
    assert_credential_read_contract(
        resolved_runtime, credential_read_roots(), provider_credential_read_exceptions("grok")
    )
    home = cache / "grok-home"
    # Keep the installed hooks and local trust choices immutable inside the run.
    # Server-refreshed policy caches are private copies and remain writable.
    protected.extend(
        str(home / name)
        for name in GROK_CONTROL_FILES
        if name not in ("managed_config.toml", "requirements.toml")
    )
    receipt_path = assets / "grok-home-source.json"
    if home.is_symlink():
        raise ValueError("Grok managed home must not be a symlink")
    if home.exists():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError("Grok managed home has no completed source receipt")
        completed = json.loads(receipt_path.read_text())
        if not isinstance(completed, dict) or completed.get("source") != str(source):
            raise ValueError("Grok managed home source changed")
        if (home / "auth.json").resolve() != auth:
            raise ValueError("Grok managed home auth target changed")
        return GrokSandboxHome(home, tuple(protected), runtime)
    home.mkdir(mode=0o700, parents=True)
    assets.mkdir(mode=0o700, parents=True, exist_ok=True)
    seed_files = (
        *(name for name in GROK_CONTROL_FILES if name not in ("hooks", "bin")),
        "managed_config.sig.json",
        "managed_identity.sig.json",
        "managed_config_cache.json",
    )
    for name in seed_files:
        original = source / name
        if original.is_symlink():
            raise ValueError(f"Grok control file must not be a symlink: {original}")
        if original.is_file():
            shutil.copyfile(original, home / name)
            (home / name).chmod(0o600)
    _declare_gobby_mcp_server(home / "config.toml")
    hooks = home / "hooks"
    hooks.mkdir(mode=0o700)
    original_hooks = source / "hooks"
    if original_hooks.is_symlink():
        raise ValueError("Grok hook directory must not be a symlink")
    if original_hooks.is_dir():
        for original in original_hooks.iterdir():
            target = hooks / original.name
            if original.suffix == ".json":
                if original.is_symlink() or not original.is_file():
                    raise ValueError(f"Grok hook JSON must be a regular file: {original}")
                shutil.copyfile(original, target)
                target.chmod(0o600)
            else:
                target.symlink_to(original, target_is_directory=original.is_dir())
    # Grok follows auth.json links before atomic publish. Keep its lock shared
    # too: refresh tokens are single-use, so private locks would race the host.
    auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth_lock.touch(mode=0o600, exist_ok=True)
    (home / "auth.json").symlink_to(auth)
    (home / "auth.json.lock").symlink_to(auth_lock)
    if (source / "mcp_credentials.json").exists():
        (home / "mcp_credentials.json").symlink_to(source / "mcp_credentials.json")
    for name in (*GROK_RUNTIME_DIRECTORIES, "skills", "personas"):
        original = source / name
        if name in GROK_RUNTIME_DIRECTORIES:
            original.mkdir(mode=0o700, parents=True, exist_ok=True)
        if original.is_dir():
            (home / name).symlink_to(original, target_is_directory=True)
    receipt_path.write_text(json.dumps({"source": str(source)}))
    receipt_path.chmod(0o600)
    return GrokSandboxHome(home, tuple(protected), runtime)


def _declare_gobby_mcp_server(config_path: Path) -> None:
    """Give the run the installed Gobby server Grok used to inherit from ~/.claude.json.

    Credential reads now deny that file, and config.toml outranks every
    Claude-compat source, so this entry replaces any operator ``gobby`` server.
    """
    import tomlkit
    from tomlkit.exceptions import ParseError

    from gobby.cli.installers.mcp_config_shared import _resolved_gobby_mcp_command

    try:
        config = tomlkit.parse(config_path.read_text() if config_path.is_file() else "")
    except ParseError as exc:
        raise ValueError(f"Grok config is not valid TOML: {config_path}") from exc
    servers = config.setdefault("mcp_servers", tomlkit.table())
    servers["gobby"] = {"command": _resolved_gobby_mcp_command(), "args": ["mcp-server"]}
    config_path.write_text(tomlkit.dumps(config))
    config_path.chmod(0o600)
