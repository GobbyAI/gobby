"""Secrets store with Fernet encryption tied to machine identity.

Provides encrypted storage for API keys and sensitive values in the hub database.
Values are encrypted using a key derived from the machine ID, so secrets
are bound to the current machine. Agents never see raw values — the daemon
resolves `$secret:NAME` references internally at connection time.
"""

import base64
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)

# Pattern for secret references: $secret:NAME
SECRET_REF_PATTERN = re.compile(r"\$secret:([A-Za-z_][A-Za-z0-9_]*)")

# Salt file location
SALT_FILE = Path("~/.gobby/.secret_salt").expanduser()

# Valid categories for secrets
VALID_CATEGORIES = {"general", "llm", "mcp_server", "memory", "integration"}


class SecretInfo:
    """Non-sensitive metadata about a stored secret."""

    __slots__ = ("id", "name", "category", "description", "created_at", "updated_at")

    def __init__(
        self,
        id: str,
        name: str,
        category: str,
        description: str | None,
        created_at: str,
        updated_at: str,
    ):
        self.id = id
        self.name = name
        self.category = category
        self.description = description
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _get_or_create_salt() -> bytes:
    """Get or create the encryption salt.

    Salt is stored in ~/.gobby/.secret_salt and generated once.
    """
    SALT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()

    salt = os.urandom(16)
    # Write with restrictive permissions
    fd = os.open(SALT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)

    logger.info("Generated new secret encryption salt")
    return salt


def _derive_fernet_key(machine_id: str, salt: bytes) -> bytes:
    """Derive a Fernet key from machine ID using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    key_bytes = kdf.derive(machine_id.encode("utf-8"))
    return base64.urlsafe_b64encode(key_bytes)


class SecretStore:
    """Encrypted secret storage backed by the hub database.

    Secrets are encrypted with a Fernet key derived from the machine ID.
    The API is write-only from outside the daemon — values can be set and
    deleted but never read through the HTTP API. Only the daemon resolves
    secrets internally via the `resolve()` method.

    All secret names are normalized to lowercase for case-insensitive matching.
    """

    def __init__(self, db: HubDatabase):
        self.db = db
        self._fernet: Fernet | None = None

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize secret name to lowercase for case-insensitive matching."""
        return name.strip().lower()

    def _get_fernet(self) -> Fernet:
        """Lazy-initialize the Fernet cipher."""
        if self._fernet is None:
            machine_id = get_machine_id()
            if not machine_id:
                raise RuntimeError("Cannot initialize secrets: machine ID unavailable")
            salt = _get_or_create_salt()
            key = _derive_fernet_key(machine_id, salt)
            self._fernet = Fernet(key)
        return self._fernet

    def set(
        self,
        name: str,
        plaintext_value: str,
        category: str = "general",
        description: str | None = None,
    ) -> SecretInfo:
        """Encrypt and store a secret (upsert).

        Args:
            name: Secret name (unique identifier)
            plaintext_value: The sensitive value to encrypt
            category: Classification category
            description: Human-readable description

        Returns:
            SecretInfo with metadata (never the value)
        """
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}")

        name = self._normalize_name(name)
        fernet = self._get_fernet()
        encrypted = fernet.encrypt(plaintext_value.encode("utf-8")).decode("utf-8")
        now = datetime.now(UTC).isoformat()

        # Check if exists
        existing = self.db.fetchone("SELECT id FROM secrets WHERE name = ?", (name,))

        if existing:
            self.db.execute(
                """UPDATE secrets
                   SET encrypted_value = ?, category = ?, description = ?, updated_at = ?
                   WHERE name = ?""",
                (encrypted, category, description, now, name),
            )
            secret_id = existing["id"]
        else:
            secret_id = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (secret_id, name, encrypted, category, description, now, now),
            )

        row = self.db.fetchone("SELECT * FROM secrets WHERE id = ?", (secret_id,))
        if row is None:
            raise ValueError(f"Secret '{name}' not found after upsert (id={secret_id})")
        return SecretInfo(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, name: str) -> str | None:
        """Decrypt and return a secret value (daemon-internal only).

        Args:
            name: Secret name

        Returns:
            Decrypted plaintext value, or None if not found
        """
        name = self._normalize_name(name)
        row = self.db.fetchone("SELECT encrypted_value FROM secrets WHERE name = ?", (name,))
        if not row:
            return None

        try:
            fernet = self._get_fernet()
            decrypted: str = fernet.decrypt(row["encrypted_value"].encode("utf-8")).decode("utf-8")
            return decrypted
        except InvalidToken:
            logger.error(f"Failed to decrypt secret '{name}' - machine ID may have changed")
            return None

    def delete(self, name: str) -> bool:
        """Delete a secret.

        Args:
            name: Secret name

        Returns:
            True if deleted, False if not found
        """
        name = self._normalize_name(name)
        row = self.db.fetchone("SELECT id FROM secrets WHERE name = ?", (name,))
        if not row:
            return False
        self.db.execute("DELETE FROM secrets WHERE name = ?", (name,))
        return True

    def list(self) -> list[SecretInfo]:
        """List all secrets (metadata only, never values).

        Returns:
            List of SecretInfo objects
        """
        rows = self.db.fetchall(
            "SELECT id, name, category, description, created_at, updated_at FROM secrets ORDER BY name"
        )
        return [
            SecretInfo(
                id=row["id"],
                name=row["name"],
                category=row["category"],
                description=row["description"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def exists(self, name: str) -> bool:
        """Check if a secret exists."""
        name = self._normalize_name(name)
        row = self.db.fetchone("SELECT 1 FROM secrets WHERE name = ?", (name,))
        return row is not None

    def resolve(self, text: str) -> str:
        """Replace $secret:NAME references with decrypted values.

        Used by the daemon to resolve secret references in config values
        (e.g., MCP server headers) before passing to external services.

        Args:
            text: String potentially containing $secret:NAME references

        Returns:
            String with secrets resolved (unresolved refs left as-is)
        """

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            value = self.get(name)
            if value is not None:
                return value
            logger.warning(f"Secret reference '$secret:{name}' not found")
            return match.group(0)

        return SECRET_REF_PATTERN.sub(_replace, text)

    def resolve_dict(self, d: dict[str, str]) -> dict[str, str]:
        """Resolve $secret:NAME references in all values of a dict.

        Args:
            d: Dictionary with potentially secret-referencing values

        Returns:
            New dictionary with secrets resolved
        """
        return {k: self.resolve(v) for k, v in d.items()}
