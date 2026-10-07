"""Per-run Qwen home: a private extension store over host configuration and auth."""

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.sandbox_control_paths import QWEN_CONTROL_FILES, assert_credential_read_contract
from gobby.paths import get_gobby_home

# Qwen rewrites these under a lock at startup, so each run gets empty ones.
_PRIVATE_STORES = ("extensions", "extension-store")


@dataclass(frozen=True)
class QwenSandboxHome:
    home: Path
    source: Path
    launch_env: dict[str, str]
    protected_write_paths: tuple[str, ...]


def prepare_qwen_sandbox_home(
    cache: Path, env: Mapping[str, str], *, assets: Path
) -> QwenSandboxHome:
    """Copy settings, link auth and customizations, keep trust and transcripts on the host.

    Only daemon-owned assets record the original host home. A resumed managed
    home therefore cannot choose a new host source through its writable files.
    """
    from gobby.agents.sandbox_policy import (
        assert_sensitive_path_contract,
        credential_read_roots,
        provider_credential_read_exceptions,
    )

    source = Path(env.get("QWEN_HOME", str(Path.home() / ".qwen"))).expanduser()
    if source.is_symlink() or cache.is_symlink():
        raise ValueError("Qwen sandbox home must use real directories")
    source = source.resolve()
    old_receipt = source.parent.parent / "assets/qwen-home-source.json"
    run_root = (get_gobby_home() / "run/sandbox").resolve()
    if source.name == "qwen-home" and source.is_relative_to(run_root):
        if old_receipt.is_symlink():
            raise ValueError("Qwen source receipt must not be a symlink")
        receipt = json.loads(old_receipt.read_text())
        if not isinstance(receipt, dict) or not isinstance(receipt.get("source"), str):
            raise ValueError("invalid Qwen source receipt")
        source = Path(receipt["source"]).resolve()
    assert_sensitive_path_contract([str(source)])
    assert_credential_read_contract(
        [str(source)], credential_read_roots(), provider_credential_read_exceptions("qwen")
    )
    settings = source / "settings.json"
    if settings.is_symlink():
        raise ValueError(f"Qwen settings must not be a symlink: {settings}")

    home = cache / "qwen-home"
    auth = home / "oauth_creds.json"
    protected = (
        *(str(home / name) for name in QWEN_CONTROL_FILES if name not in _PRIVATE_STORES),
        # A token refresh renames over this path rather than following the link,
        # which would leave a credential copy in the run and strand the host token.
        str(auth),
    )
    # Gobby trusts the workspace in the host file after this preparation, and
    # reads transcripts from the host runtime directory.
    launch_env = {"QWEN_HOME": str(home)}
    for variable, default in (
        ("QWEN_RUNTIME_DIR", source),
        ("QWEN_CODE_TRUSTED_FOLDERS_PATH", source / "trustedFolders.json"),
    ):
        if not env.get(variable):
            launch_env[variable] = str(default)
    prepared = QwenSandboxHome(home, source, launch_env, protected)

    receipt_path = assets / "qwen-home-source.json"
    if home.is_symlink():
        raise ValueError("Qwen managed home must not be a symlink")
    if home.exists():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError("Qwen managed home has no completed source receipt")
        completed = json.loads(receipt_path.read_text())
        if not isinstance(completed, dict) or completed.get("source") != str(source):
            raise ValueError("Qwen managed home source changed")
        return prepared
    home.mkdir(mode=0o700, parents=True)
    assets.mkdir(mode=0o700, parents=True, exist_ok=True)
    if settings.is_file():
        shutil.copyfile(settings, home / "settings.json")
        (home / "settings.json").chmod(0o600)
    auth.symlink_to(source / "oauth_creds.json")
    for name in ("agents", "skills", "commands"):
        if (source / name).is_dir():
            (home / name).symlink_to(source / name, target_is_directory=True)
    receipt_path.write_text(json.dumps({"source": str(source)}))
    receipt_path.chmod(0o600)
    return prepared
