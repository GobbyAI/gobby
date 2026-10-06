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
    runtime = tuple(str(source / name) for name in GROK_RUNTIME_DIRECTORIES)
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
    # Links expose only the active provider's existing read necessities. Atomic
    # auth refresh replaces the per-run link instead of modifying the host file.
    for name in ("auth.json", "mcp_credentials.json"):
        if (source / name).exists():
            (home / name).symlink_to(source / name)
    for name in (*GROK_RUNTIME_DIRECTORIES, "skills", "personas"):
        original = source / name
        if name in GROK_RUNTIME_DIRECTORIES:
            original.mkdir(mode=0o700, parents=True, exist_ok=True)
        if original.is_dir():
            (home / name).symlink_to(original, target_is_directory=True)
    receipt_path.write_text(json.dumps({"source": str(source)}))
    receipt_path.chmod(0o600)
    return GrokSandboxHome(home, tuple(protected), runtime)
