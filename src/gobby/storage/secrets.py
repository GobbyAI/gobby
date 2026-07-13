"""Secrets store with daemon-local envelope encryption.

Secret values are encrypted with a random data-encryption key (DEK). The DEK
is wrapped by a key-encryption key (KEK), and only the wrapped DEK is stored in
the hub. The daemon resolves ``$secret:NAME`` references internally; clients
never receive raw secret values or KEK material.
"""

import base64
import hashlib
import logging
import os
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from gobby.paths import get_gobby_home
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secret_names import (
    SECRET_REF_PATTERN,
    normalize_and_validate_secret_name,
    normalize_secret_name,
)
from gobby.utils.datetime import datetime_to_required_iso, require_stored_datetime, utc_now
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)

_SALT_FILENAME = ".secret_salt"
_KEK_FILENAME = ".secret_kek"
# Every file holding secret key material under ~/.gobby. Anything that
# provisions an alternate GOBBY_HOME or snapshots ~/.gobby must carry all of
# these, or envelope decryption breaks (Rust gcore reads GOBBY_HOME/.secret_kek).
SECRET_MATERIAL_FILENAMES: tuple[str, ...] = (_SALT_FILENAME, _KEK_FILENAME)

# These B105 suppressions are scoped to metadata literals, never secret values.
# Stable key identifier, not a credential.
SECRET_KEY_ID = "default"  # nosec B105
POSTURE_KEY_FILE = "key_file"
# Secret-posture enum value, not a credential.
POSTURE_SCRYPT_PASSPHRASE = "scrypt_passphrase"  # nosec B105
# Environment variable name, not a credential.
SECRET_KEK_PASSPHRASE_ENV = "GOBBY_SECRET_KEK_PASSPHRASE"  # nosec B105

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

VALID_CATEGORIES = {"general", "llm", "mcp_server", "memory", "integration"}

_LEGACY_FERNET_KEY_CACHE_MAX_SIZE = 8
_legacy_fernet_key_cache: OrderedDict[bytes, bytes] = OrderedDict()
_legacy_fernet_key_cache_lock = threading.Lock()


class SecretKeyUnavailable(RuntimeError):
    """Raised when the configured KEK cannot be loaded or cannot unwrap the DEK."""


class InvalidSecretSaltError(RuntimeError):
    """Raised when the legacy secret salt does not have the required length."""


class SecretDecryptionError(RuntimeError):
    """Raised when stored secret ciphertext cannot be decrypted."""

    def __init__(self, secret_identifier: str) -> None:
        self.secret_identifier = secret_identifier
        super().__init__(f"Failed to decrypt configured secret {secret_identifier}")


class SecretMigrationError(RuntimeError):
    """Raised when required legacy secrets cannot be migrated."""

    def __init__(self, report: "SecretMigrationReport") -> None:
        self.report = report
        failed = ", ".join(entry.name for entry in report.entries if entry.status == "failed")
        super().__init__(f"Required secret migration failed: {failed or 'unknown secret'}")


@dataclass(frozen=True, slots=True)
class SecretMigrationEntry:
    """One row in a legacy secret migration report."""

    name: str
    status: str
    required: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SecretMigrationReport:
    """Summary of a legacy secret migration or dry run."""

    dry_run: bool
    key_material_created: bool
    entries: list[SecretMigrationEntry]

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def migrated(self) -> int:
        return sum(1 for entry in self.entries if entry.status in {"migrated", "would_migrate"})

    @property
    def skipped(self) -> int:
        return sum(1 for entry in self.entries if entry.status in {"skipped", "would_skip"})

    @property
    def failed(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "key_material_created": self.key_material_created,
            "total": self.total,
            "migrated": self.migrated,
            "skipped": self.skipped,
            "failed": self.failed,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _safe_secret_identifier(normalized_name: str) -> str:
    """Return a deterministic non-reversible identifier for logs."""
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


class SecretInfo:
    """Non-sensitive metadata about a stored secret."""

    __slots__ = ("id", "name", "category", "description", "created_at", "updated_at")

    def __init__(
        self,
        id: str,
        name: str,
        category: str,
        description: str | None,
        created_at: datetime | str,
        updated_at: datetime | str,
    ):
        self.id = id
        self.name = name
        self.category = category
        self.description = description
        self.created_at = require_stored_datetime(created_at, "created_at")
        self.updated_at = require_stored_datetime(updated_at, "updated_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "created_at": datetime_to_required_iso(self.created_at),
            "updated_at": datetime_to_required_iso(self.updated_at),
        }


def _read_secret_salt(salt_file: Path) -> bytes:
    salt = salt_file.read_bytes()
    if len(salt) != 16:
        raise InvalidSecretSaltError(
            f"Invalid secret salt file {salt_file}: expected 16 bytes, found {len(salt)}"
        )
    return salt


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("Failed to write secret material")
        remaining = remaining[written:]


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_private_file(path: Path, data: bytes) -> bool:
    """Durably publish a complete private file without replacing a racing winner."""
    temp_file = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    published = False
    try:
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

        try:
            # A hard link is an atomic, no-clobber publication within one directory.
            # os.rename()/os.replace() would let a later racer overwrite the winner.
            os.link(temp_file, path)
        except FileExistsError:
            return False
        published = True
        return True
    finally:
        try:
            temp_file.unlink(missing_ok=True)
        finally:
            if published:
                _fsync_directory(path.parent)


def _publish_secret_salt(salt_file: Path, salt: bytes) -> bool:
    """Publish a complete salt without replacing a racing process's winner."""
    return _publish_private_file(salt_file, salt)


def _get_or_create_salt() -> bytes:
    """Get or atomically create the legacy machine_id encryption salt."""
    salt_file = get_gobby_home() / _SALT_FILENAME
    salt_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        return _read_secret_salt(salt_file)
    except FileNotFoundError:
        pass

    salt = os.urandom(16)
    if _publish_secret_salt(salt_file, salt):
        logger.info("Generated new legacy secret encryption salt")
        return salt

    return _read_secret_salt(salt_file)


def _derive_fernet_key_uncached(machine_id: str, salt: bytes) -> bytes:
    """Derive the legacy Fernet key from machine ID using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    key_bytes = kdf.derive(machine_id.encode("utf-8"))
    return base64.urlsafe_b64encode(key_bytes)


def _legacy_fernet_key_cache_key(machine_id: str, salt: bytes) -> bytes:
    """Return a fixed-size cache key without retaining raw derivation inputs."""
    machine_id_bytes = machine_id.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(machine_id_bytes).to_bytes(8, "big"))
    digest.update(machine_id_bytes)
    digest.update(len(salt).to_bytes(8, "big"))
    digest.update(salt)
    return digest.digest()


def _derive_fernet_key(machine_id: str, salt: bytes) -> bytes:
    """Return a process-cached legacy Fernet key for stable derivation inputs."""
    cache_key = _legacy_fernet_key_cache_key(machine_id, salt)
    with _legacy_fernet_key_cache_lock:
        cached = _legacy_fernet_key_cache.get(cache_key)
        if cached is not None:
            _legacy_fernet_key_cache.move_to_end(cache_key)
            return cached

        # Keep derivation inside the lock: concurrent first users must not each
        # pay the 600k-iteration PBKDF2 cost for the same process-stable inputs.
        key = _derive_fernet_key_uncached(machine_id, salt)
        _legacy_fernet_key_cache[cache_key] = key
        if len(_legacy_fernet_key_cache) > _LEGACY_FERNET_KEY_CACHE_MAX_SIZE:
            _legacy_fernet_key_cache.popitem(last=False)
        return key


def _clear_legacy_fernet_key_cache() -> None:
    """Clear the bounded process cache (primarily for isolated tests)."""
    with _legacy_fernet_key_cache_lock:
        _legacy_fernet_key_cache.clear()


def _derive_scrypt_fernet_key(
    passphrase: str,
    salt: bytes,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    """Derive a Fernet KEK from a passphrase using scrypt."""
    kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _read_kek_file(kek_file: Path) -> bytes:
    key = kek_file.read_bytes().strip()
    try:
        Fernet(key)
    except (TypeError, ValueError) as exc:
        raise SecretKeyUnavailable(f"Invalid secret KEK file: {kek_file}") from exc
    try:
        kek_file.chmod(0o600)
    except OSError:
        logger.warning("Could not enforce 0600 permissions on %s", kek_file)
    return key


def _get_or_create_kek_file_key() -> bytes:
    """Return the default key-file KEK, creating it with 0600 permissions."""
    kek_file = get_gobby_home() / _KEK_FILENAME
    kek_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _read_kek_file(kek_file)
    except FileNotFoundError:
        pass

    key = Fernet.generate_key()
    if _publish_private_file(kek_file, key):
        logger.info("Generated new secret KEK file")
        return key

    return _read_kek_file(kek_file)


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode_bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _normalize_posture(posture: str) -> str:
    normalized = posture.strip().lower().replace("-", "_")
    if normalized not in {POSTURE_KEY_FILE, POSTURE_SCRYPT_PASSPHRASE}:
        raise ValueError(
            f"Invalid secret KEK posture '{posture}'. "
            f"Expected {POSTURE_KEY_FILE!r} or {POSTURE_SCRYPT_PASSPHRASE!r}."
        )
    return normalized


class SecretStore:
    """Encrypted secret storage backed by the hub database.

    Secret values are encrypted by a random DEK. The DEK is wrapped by a KEK
    and stored in ``secret_key_material``. Only daemon-internal code can call
    ``get()``/``resolve()``; HTTP APIs expose write-only secret management.

    Legacy migration's 600k-iteration PBKDF2 key uses a synchronized, bounded
    process cache, so repeated store instances reuse the process-stable result.

    All secret names are normalized to lowercase for case-insensitive matching.
    """

    def __init__(self, db: HubDatabase, *, kek_passphrase: str | None = None):
        self.db = db
        self.kek_passphrase = kek_passphrase
        self._fernet: Fernet | None = None

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize secret name to lowercase for case-insensitive matching."""
        return normalize_secret_name(name)

    @classmethod
    def find_secret_references(cls, values: Iterable[Any]) -> set[str]:
        """Return normalized explicit ``$secret:NAME`` references from strings."""
        refs: set[str] = set()

        def visit(value: Any) -> None:
            if not isinstance(value, str):
                if isinstance(value, Mapping):
                    for nested in value.values():
                        visit(nested)
                elif isinstance(value, Iterable):
                    for nested in value:
                        visit(nested)
                return
            refs.update(
                cls._normalize_name(match.group(1)) for match in SECRET_REF_PATTERN.finditer(value)
            )

        for value in values:
            visit(value)
        return refs

    def _load_key_material(self) -> Any | None:
        return self.db.fetchone(
            """SELECT id, wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p
               FROM secret_key_material
               WHERE id = %s""",
            (SECRET_KEY_ID,),
        )

    def _passphrase(self) -> str:
        passphrase = self.kek_passphrase or os.environ.get(SECRET_KEK_PASSPHRASE_ENV)
        if not passphrase:
            raise SecretKeyUnavailable(
                f"Secret KEK passphrase posture requires {SECRET_KEK_PASSPHRASE_ENV}"
            )
        return passphrase

    def _kek_fernet(
        self,
        posture: str,
        *,
        salt_text: str | None = None,
        passphrase: str | None = None,
        n: int | None = None,
        r: int | None = None,
        p: int | None = None,
    ) -> tuple[Fernet, str | None, int | None, int | None, int | None]:
        posture = _normalize_posture(posture)
        if posture == POSTURE_KEY_FILE:
            return Fernet(_get_or_create_kek_file_key()), None, None, None, None

        salt = os.urandom(16) if salt_text is None else _decode_bytes(salt_text)
        resolved_n = n or SCRYPT_N
        resolved_r = r or SCRYPT_R
        resolved_p = p or SCRYPT_P
        key = _derive_scrypt_fernet_key(
            passphrase or self._passphrase(),
            salt,
            n=resolved_n,
            r=resolved_r,
            p=resolved_p,
        )
        return Fernet(key), _encode_bytes(salt), resolved_n, resolved_r, resolved_p

    def _wrap_dek(
        self,
        dek: bytes,
        *,
        posture: str,
        passphrase: str | None = None,
    ) -> tuple[str, str | None, int | None, int | None, int | None]:
        kek, salt_text, kdf_n, kdf_r, kdf_p = self._kek_fernet(
            posture,
            passphrase=passphrase,
        )
        return kek.encrypt(dek).decode("utf-8"), salt_text, kdf_n, kdf_r, kdf_p

    def _unwrap_dek(self, row: Any, *, passphrase: str | None = None) -> bytes:
        posture = str(row["kek_posture"])
        try:
            kek, _salt, _n, _r, _p = self._kek_fernet(
                posture,
                salt_text=row["kek_salt"],
                passphrase=passphrase,
                n=row["kek_kdf_n"],
                r=row["kek_kdf_r"],
                p=row["kek_kdf_p"],
            )
            return kek.decrypt(str(row["wrapped_dek"]).encode("utf-8"))
        except InvalidToken as exc:
            raise SecretKeyUnavailable(
                "Secret DEK cannot be unwrapped with configured KEK"
            ) from exc

    def _upsert_key_material(
        self,
        dek: bytes,
        *,
        posture: str,
        passphrase: str | None = None,
        executor: Any | None = None,
    ) -> None:
        posture = _normalize_posture(posture)
        wrapped_dek, salt_text, kdf_n, kdf_r, kdf_p = self._wrap_dek(
            dek,
            posture=posture,
            passphrase=passphrase,
        )
        now = utc_now()
        target = executor if executor is not None else self.db
        target.execute(
            """INSERT INTO secret_key_material (
                   id, wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p,
                   created_at, updated_at
               )
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   wrapped_dek = EXCLUDED.wrapped_dek,
                   kek_posture = EXCLUDED.kek_posture,
                   kek_salt = EXCLUDED.kek_salt,
                   kek_kdf_n = EXCLUDED.kek_kdf_n,
                   kek_kdf_r = EXCLUDED.kek_kdf_r,
                   kek_kdf_p = EXCLUDED.kek_kdf_p,
                   updated_at = EXCLUDED.updated_at""",
            (SECRET_KEY_ID, wrapped_dek, posture, salt_text, kdf_n, kdf_r, kdf_p, now, now),
        )

    def _insert_key_material_if_absent(
        self,
        dek: bytes,
        *,
        posture: str,
        passphrase: str | None,
        executor: Any,
    ) -> bool:
        posture = _normalize_posture(posture)
        wrapped_dek, salt_text, kdf_n, kdf_r, kdf_p = self._wrap_dek(
            dek,
            posture=posture,
            passphrase=passphrase,
        )
        now = utc_now()
        cursor = executor.execute(
            """INSERT INTO secret_key_material (
                   id, wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p,
                   created_at, updated_at
               )
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING
               RETURNING id""",
            (SECRET_KEY_ID, wrapped_dek, posture, salt_text, kdf_n, kdf_r, kdf_p, now, now),
        )
        return cursor.fetchone() is not None

    def _legacy_fernet(self) -> Fernet:
        machine_id = get_machine_id()
        if not machine_id:
            raise RuntimeError("Cannot migrate legacy secrets: machine ID unavailable")
        return Fernet(_derive_fernet_key(machine_id, _get_or_create_salt()))

    def _initialize_envelope(
        self,
        *,
        required_secret_names: set[str],
        dry_run: bool,
        posture: str,
        passphrase: str | None = None,
    ) -> tuple[bytes | None, SecretMigrationReport]:
        rows = self.db.fetchall("SELECT name, encrypted_value FROM secrets ORDER BY name")
        dek = Fernet.generate_key()
        dek_fernet = Fernet(dek)
        entries: list[SecretMigrationEntry] = []
        migrated_values: list[tuple[str, str]] = []

        legacy_fernet: Fernet | None = None
        legacy_error: str | None = None
        if rows:
            try:
                legacy_fernet = self._legacy_fernet()
            except RuntimeError as exc:
                legacy_error = str(exc)

        for row in rows:
            name = self._normalize_name(str(row["name"]))
            required = name in required_secret_names
            if legacy_fernet is None:
                status = "failed" if required else ("would_skip" if dry_run else "skipped")
                entries.append(
                    SecretMigrationEntry(
                        name=name,
                        status=status,
                        required=required,
                        reason=legacy_error or "legacy key unavailable",
                    )
                )
                continue

            try:
                plaintext = legacy_fernet.decrypt(str(row["encrypted_value"]).encode("utf-8"))
            except InvalidToken:
                status = "failed" if required else ("would_skip" if dry_run else "skipped")
                entries.append(
                    SecretMigrationEntry(
                        name=name,
                        status=status,
                        required=required,
                        reason="legacy token could not be decrypted",
                    )
                )
                continue

            status = "would_migrate" if dry_run else "migrated"
            entries.append(SecretMigrationEntry(name=name, status=status, required=required))
            migrated_values.append((name, dek_fernet.encrypt(plaintext).decode("utf-8")))

        report = SecretMigrationReport(
            dry_run=dry_run,
            key_material_created=not dry_run,
            entries=entries,
        )
        if report.failed:
            raise SecretMigrationError(report)
        if dry_run:
            return None, report

        with self.db.transaction() as txn:
            key_material_created = self._insert_key_material_if_absent(
                dek,
                posture=posture,
                passphrase=passphrase,
                executor=txn,
            )
            if key_material_created:
                for name, encrypted_value in migrated_values:
                    txn.execute(
                        "UPDATE secrets SET encrypted_value = %s, updated_at = %s WHERE name = %s",
                        (encrypted_value, utc_now(), name),
                    )

        if not key_material_created:
            winner = self._load_key_material()
            if winner is None:
                raise RuntimeError(
                    "Concurrent secret envelope initialization did not publish a key"
                )
            winner_dek = self._unwrap_dek(winner, passphrase=passphrase)
            concurrent_report = SecretMigrationReport(
                dry_run=False,
                key_material_created=False,
                entries=entries,
            )
            self._fernet = Fernet(winner_dek)
            return winner_dek, concurrent_report

        for entry in entries:
            if entry.status == "skipped":
                logger.warning(
                    "Skipping legacy secret during envelope migration",
                    extra={
                        "secret": _safe_secret_identifier(entry.name),
                        "reason": entry.reason,
                    },
                )

        self._fernet = Fernet(dek)
        return dek, report

    def _get_dek(self) -> bytes:
        row = self._load_key_material()
        if row is not None:
            return self._unwrap_dek(row)
        legacy_row = self.db.fetchone("SELECT 1 FROM secrets LIMIT 1")
        if legacy_row:
            raise RuntimeError(
                "Secret envelope key material is missing while legacy secrets exist; "
                "run ensure_ready() before accessing secrets."
            )
        dek, _report = self._initialize_envelope(
            required_secret_names=set(),
            dry_run=False,
            posture=POSTURE_KEY_FILE,
        )
        if dek is None:
            raise RuntimeError("Secret envelope was not initialized")
        return dek

    def _get_fernet(self) -> Fernet:
        """Lazy-initialize the DEK Fernet cipher."""
        if self._fernet is None:
            self._fernet = Fernet(self._get_dek())
        return self._fernet

    def ensure_ready(self, *, required_secret_names: Iterable[str] = ()) -> SecretMigrationReport:
        """Initialize envelope metadata and migrate legacy rows if needed."""
        required = {self._normalize_name(name) for name in required_secret_names}
        if self._load_key_material() is not None:
            self._get_fernet()
            return SecretMigrationReport(dry_run=False, key_material_created=False, entries=[])
        _dek, report = self._initialize_envelope(
            required_secret_names=required,
            dry_run=False,
            posture=POSTURE_KEY_FILE,
        )
        return report

    def migrate_legacy_machine_id_secrets(
        self,
        *,
        dry_run: bool = False,
        required_secret_names: Iterable[str] = (),
        posture: str = POSTURE_KEY_FILE,
        passphrase: str | None = None,
    ) -> SecretMigrationReport:
        """Migrate legacy machine_id-derived rows into the envelope scheme."""
        required = {self._normalize_name(name) for name in required_secret_names}
        if self._load_key_material() is not None:
            if not dry_run:
                self._get_fernet()
            return SecretMigrationReport(dry_run=dry_run, key_material_created=False, entries=[])
        _dek, report = self._initialize_envelope(
            required_secret_names=required,
            dry_run=dry_run,
            posture=posture,
            passphrase=passphrase,
        )
        return report

    def current_kek_posture(self) -> str | None:
        """Return the active KEK posture, or None before first initialization."""
        row = self._load_key_material()
        if row is None:
            return None
        return str(row["kek_posture"])

    def set_kek_posture(self, posture: str, *, passphrase: str | None = None) -> None:
        """Re-wrap the DEK with a new KEK posture without re-encrypting secrets."""
        posture = _normalize_posture(posture)
        row = self._load_key_material()
        if row is None:
            dek, report = self._initialize_envelope(
                required_secret_names=set(),
                dry_run=False,
                posture=posture,
                passphrase=passphrase,
            )
            if dek is None:
                raise RuntimeError("Secret envelope was not initialized")
            if not report.key_material_created:
                self._upsert_key_material(dek, posture=posture, passphrase=passphrase)
        else:
            dek = self._unwrap_dek(row)
            self._upsert_key_material(dek, posture=posture, passphrase=passphrase)
        self._fernet = Fernet(dek)

    def set(
        self,
        name: str,
        plaintext_value: str,
        category: str = "general",
        description: str | None = None,
    ) -> SecretInfo:
        """Encrypt and store a secret (upsert)."""
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}")

        name = normalize_and_validate_secret_name(name)
        fernet = self._get_fernet()
        encrypted = fernet.encrypt(plaintext_value.encode("utf-8")).decode("utf-8")
        now = utc_now()
        row = self.db.fetchone(
            """INSERT INTO secrets (
                   id, name, encrypted_value, category, description, created_at, updated_at
               )
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (name) DO UPDATE SET
                   encrypted_value = EXCLUDED.encrypted_value,
                   category = EXCLUDED.category,
                   description = EXCLUDED.description,
                   updated_at = EXCLUDED.updated_at
               RETURNING id, name, category, description, created_at, updated_at""",
            (str(uuid.uuid4()), name, encrypted, category, description, now, now),
        )
        if row is None:
            raise RuntimeError("Secret upsert did not return a row")
        return SecretInfo(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, name: str) -> str | None:
        """Decrypt and return a secret value (daemon-internal only)."""
        name = self._normalize_name(name)
        row = self.db.fetchone("SELECT encrypted_value FROM secrets WHERE name = %s", (name,))
        if not row:
            return None

        try:
            fernet = self._get_fernet()
            decrypted: str = fernet.decrypt(row["encrypted_value"].encode("utf-8")).decode("utf-8")
            return decrypted
        except InvalidToken:
            raise SecretDecryptionError(_safe_secret_identifier(name)) from None

    def delete(self, name: str) -> bool:
        """Delete a secret."""
        name = self._normalize_name(name)
        row = self.db.fetchone("SELECT id FROM secrets WHERE name = %s", (name,))
        if not row:
            return False
        self.db.execute("DELETE FROM secrets WHERE name = %s", (name,))
        return True

    def list(self) -> list[SecretInfo]:
        """List all secrets (metadata only, never values)."""
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
        row = self.db.fetchone("SELECT 1 FROM secrets WHERE name = %s", (name,))
        return row is not None

    def resolve(self, text: str) -> str:
        """Replace $secret:NAME references with decrypted values."""

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                value = self.get(name)
            except SecretDecryptionError as exc:
                logger.error(
                    "Configured secret reference could not be decrypted: %s",
                    exc.secret_identifier,
                    extra={"reason": "invalid_token"},
                )
                return ""
            if value is not None:
                return value
            logger.warning(
                "Configured secret reference not found: %s",
                _safe_secret_identifier(self._normalize_name(name)),
            )
            return ""

        return SECRET_REF_PATTERN.sub(_replace, text)

    def resolve_dict(self, d: dict[str, str]) -> dict[str, str]:
        """Resolve $secret:NAME references in all values of a dict."""
        return {k: self.resolve(v) for k, v in d.items()}
