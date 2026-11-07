"""Gobby steps for the isolated daemon, run by the runtime's own interpreter.

The harness process imports the epic tree, so every gobby import happens here, under
tools/gobby/venv/bin/python with -I. Each operation first checks that this process's gobby
resolves under sources/gobby, then does its work. The parent sends a JSON payload on stdin
and reads one JSON line from stdout. Only the standard library is imported at module level.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class Refusal(Exception):
    """A check failed; the parent stops the step with this message."""


def verify(root: Path) -> dict[str, Any]:
    import gobby
    from gobby.utils.native_bin import resolve_native_bin

    if not Path(gobby.__file__).resolve().is_relative_to((root / "sources/gobby").resolve()):
        raise Refusal(f"gobby resolves outside sources/gobby: {gobby.__file__}")
    gdaemon = resolve_native_bin("gdaemon")
    if gdaemon != str(root / "gobby-home/bin/gdaemon"):
        raise Refusal(f"gdaemon resolves to {gdaemon}")
    return {"gobby": gobby.__file__, "gdaemon": gdaemon}


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
