"""API key storage and local-daemon key adoption."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gobby.config.bootstrap_io import (
    bootstrap_path as canonical_bootstrap_path,
)
from gobby.config.bootstrap_io import (
    publish_bootstrap_yaml_locked,
    read_bootstrap_yaml,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.users import LocalUserManager
from gobby.utils import api_key_format
from gobby.utils.durable_file import exclusive_file_lock

logger = logging.getLogger(__name__)

LOCAL_DAEMON_KEY_LABEL = "local daemon"
_COLUMNS = "id, user_id, machine_id, key_hint, label, created_at, last_used_at, revoked_at"


@dataclass(frozen=True, slots=True)
class ApiKey:
    """An ``api_keys`` row without its hash."""

    id: str
    user_id: str
    machine_id: str
    hint: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ApiKey:
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            machine_id=str(row["machine_id"]),
            hint=row["key_hint"],
            label=row["label"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
        )


def _uuid_or_none(value: str) -> str | None:
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


class ApiKeyManager:
    """Mint, list and revoke API keys. Key resolution is gdaemon's."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def mint(self, user_id: str, machine_id: str, label: str) -> tuple[str, ApiKey]:
        """Insert a new key and return its plaintext, which is never stored."""
        plaintext = api_key_format.generate()
        row = self.db.fetchone(
            f"""
            INSERT INTO api_keys (id, user_id, machine_id, key_hash, key_hint, label)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                str(uuid.uuid4()),
                str(uuid.UUID(user_id.strip())),
                str(uuid.UUID(machine_id.strip())),
                api_key_format.hash(plaintext),
                api_key_format.hint(plaintext),
                label,
            ),
        )
        if row is None:
            raise RuntimeError("API key insert returned no row")
        return plaintext, ApiKey.from_row(row)

    def list_for_user(self, user_id: str) -> list[ApiKey]:
        rows = self.db.fetchall(
            f"SELECT {_COLUMNS} FROM api_keys WHERE user_id = %s ORDER BY created_at, id",
            (str(uuid.UUID(user_id.strip())),),
        )
        return [ApiKey.from_row(row) for row in rows]

    def revoke(self, key_id: str, user_id: str) -> bool:
        """Revoke a key the user owns. False when no such key is theirs."""
        normalized_id = _uuid_or_none(key_id)
        if normalized_id is None:
            return False
        row = self.db.fetchone(
            """
            UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now())
            WHERE id = %s AND user_id = %s
            RETURNING id
            """,
            (normalized_id, str(uuid.UUID(user_id.strip()))),
        )
        return row is not None

    def is_live_for_machine(self, key_id: str, plaintext: str, machine_id: str) -> bool:
        normalized_id = _uuid_or_none(key_id)
        if normalized_id is None:
            return False
        row = self.db.fetchone(
            """
            SELECT 1 FROM api_keys
            WHERE id = %s AND key_hash = %s AND machine_id = %s AND revoked_at IS NULL
            """,
            (normalized_id, api_key_format.hash(plaintext), str(uuid.UUID(machine_id.strip()))),
        )
        return row is not None


def ensure_local_api_key(database: HubDatabase, machine_id: str, bootstrap_path: Path) -> None:
    """Make the local bootstrap name a live key bound to this machine.

    A key is minted only when the bootstrap names none that is live here. The
    bootstrap rename is the commit point: a key whose publication may have landed
    stays live, and one that provably did not is revoked.
    """
    if bootstrap_path.name != "bootstrap.yaml":
        logger.warning(
            "Skipping local API key adoption: %s is a legacy config with no sibling "
            "bootstrap.yaml; run `gobby install` to migrate to %s",
            bootstrap_path,
            canonical_bootstrap_path(),
        )
        return
    keys = ApiKeyManager(database)
    with exclusive_file_lock(bootstrap_path):
        data = read_bootstrap_yaml(bootstrap_path)
        if data.get("datastore_mode", "local") != "local":
            return
        api_key, api_key_id = data.get("api_key"), data.get("api_key_id")
        if (
            isinstance(api_key, str)
            and isinstance(api_key_id, str)
            and keys.is_live_for_machine(api_key_id, api_key, machine_id)
        ):
            return
        user = LocalUserManager(database).require_sole_user()
        plaintext, minted = keys.mint(user.id, machine_id, LOCAL_DAEMON_KEY_LABEL)
        data["api_key"] = plaintext
        data["api_key_id"] = minted.id
        try:
            publish_bootstrap_yaml_locked(bootstrap_path, data)
        except Exception:
            try:
                committed = read_bootstrap_yaml(bootstrap_path).get("api_key_id") == minted.id
            except Exception:
                logger.warning(
                    "Bootstrap readback failed after publishing API key %s; leaving it live",
                    minted.id,
                    exc_info=True,
                )
                committed = True
            if not committed:
                keys.revoke(minted.id, user.id)
            raise
