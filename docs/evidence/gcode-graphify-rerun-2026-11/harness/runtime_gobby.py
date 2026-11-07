"""Gobby steps for the isolated daemon, run by the runtime's own interpreter.

The harness process imports the epic tree, so every gobby import happens here, under
tools/gobby/venv/bin/python with -I. Each operation first checks that this process's gobby
resolves under sources/gobby, then does its work. The parent sends a JSON payload on stdin
and reads one JSON line from stdout. Only the standard library is imported at module level.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


class Refusal(Exception):
    """A check failed; the parent stops the step with this message."""


def verify_gobby(root: Path) -> dict[str, Any]:
    """Refuse unless this interpreter's gobby is the runtime's own source."""
    import gobby

    if not Path(gobby.__file__).resolve().is_relative_to((root / "sources/gobby").resolve()):
        raise Refusal(f"gobby resolves outside sources/gobby: {gobby.__file__}")
    return {"gobby": gobby.__file__}


def verify(root: Path) -> dict[str, Any]:
    """Refuse unless gobby is the runtime's and gdaemon resolves to the installed binary."""
    from gobby.utils.native_bin import resolve_native_bin

    result = verify_gobby(root)
    gdaemon = resolve_native_bin("gdaemon")
    if gdaemon != str(root / "gobby-home/bin/gdaemon"):
        raise Refusal(f"gdaemon resolves to {gdaemon}")
    return {**result, "gdaemon": gdaemon}


def known_config_values(values: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    # Keys the current registry no longer defines are dropped; the receipt lists each one.
    from gobby.config.registry import CONFIG_REGISTRY, UnknownConfigKeyError

    kept: dict[str, object] = {}
    dropped: list[str] = []
    for key, value in values.items():
        try:
            CONFIG_REGISTRY.resolve(key)
        except UnknownConfigKeyError:
            dropped.append(key)
        else:
            kept[key] = value
    return kept, dropped


def identity(root: Path, _payload: dict[str, Any]) -> dict[str, Any]:
    return verify(root)


def binary_set_files(root: Path, _payload: dict[str, Any]) -> dict[str, Any]:
    # gdaemon is installed by this very step, so only the gobby check runs here.
    verify_gobby(root)
    from gobby.utils.native_bin import IDENTITY_STAMP_NAME, SET_MEMBERS, native_bin_name

    names = [native_bin_name(member) for member in SET_MEMBERS] + [IDENTITY_STAMP_NAME]
    return {"set_files": names}


def module_origins(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    verify(root)
    origins: dict[str, str | None] = {}
    for name in payload["modules"]:
        try:
            spec = importlib.util.find_spec(name)
        except ImportError:
            spec = None
        origins[name] = spec.origin if spec is not None else None
    return {"origins": origins}


def prepare_daemon(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    verify(root)
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.schema_contract import apply_schema

    dsn = payload["dsn"]
    apply_schema(dsn, schema="public")
    db = PostgresHubDatabase(dsn)
    try:
        mutations = ConfigMutations(db)
        values, dropped_keys = known_config_values(payload["values"])
        revision = mutations.patch_internal(
            expected_revision=mutations.repository.current_revision(),
            source="wiki-bakeoff-22948",
            patch=ConfigPatch(
                values=values,
                secrets={
                    "databases.falkordb.password": SecretUpdate(
                        plaintext=payload["falkordb_password"]
                    ),
                },
            ),
        )
    finally:
        db.close()
    return {"revision": revision.revision, "dropped_keys": dropped_keys}


def write_account_once(path: Path, account: dict[str, Any]) -> None:
    """Record the account before its user exists; an account file that is already there is kept.

    A crash after this write and before users.create leaves the recorded password in place, so
    the next run creates the user from it.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(account, indent=2) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def prepare_identity(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    verify(root)
    from gobby.cli.install_identity import ensure_install_identity
    from gobby.identity import hash_password
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.users import LocalUserManager

    db = PostgresHubDatabase(payload["database_url"])
    try:
        users = LocalUserManager(db)
        existing = users.list()
        if len(existing) > 1:
            raise Refusal("ambiguous isolated installation identity")
        created = not existing
        if created:
            account = payload["account"]
            write_account_once(Path(payload["account_path"]), account)
            users.create(
                name="Wiki bakeoff 22948",
                email=account["email"],
                password_hash=hash_password(account["password"]),
            )
        user = ensure_install_identity(db, no_interactive=True)
    finally:
        db.close()
    return {"user_id": str(user.id), "email": user.email, "created": created}


OPERATIONS = {
    "identity": identity,
    "binary-set-files": binary_set_files,
    "module-origins": module_origins,
    "prepare-daemon": prepare_daemon,
    "prepare-identity": prepare_identity,
}


def main(argv: list[str]) -> int:
    root = Path(argv[2])
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        result = OPERATIONS[argv[1]](root, payload)
    except Refusal as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
