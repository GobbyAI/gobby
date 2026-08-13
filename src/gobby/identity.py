"""Account identity validation and password hashing."""

import base64
import binascii
import re
import secrets

from argon2.low_level import ARGON2_VERSION, Type, hash_secret, hash_secret_raw

_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_COST = 65536
_ARGON2_PARALLELISM = 4
_ARGON2_HASH_LEN = 32
_ARGON2_SALT_LEN = 16
_ARGON2_PARAMETERS = f"m={_ARGON2_MEMORY_COST},t={_ARGON2_TIME_COST},p={_ARGON2_PARALLELISM}"
_INVALID_PASSWORD_DIGEST = bytes([0xFF]) * _ARGON2_HASH_LEN
_EMPTY_PASSWORD_DIGEST = bytes(_ARGON2_HASH_LEN)
_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

# Valid canonical hash used to keep unknown-email login attempts in the same
# Argon2 work class as wrong-password attempts.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$Z29iYnktZHVtbXktc2FsdA$"
    "DrxHcX6/u8pE5u8V9MMmai5FtT2HpjRCeG1EG5zvw+U"
)


def normalize_user_name(name: str) -> str:
    """Trim and validate an account display name."""
    normalized = name.strip()
    if not normalized:
        raise ValueError("User name must not be blank")
    return normalized


def normalize_user_email(email: str) -> str:
    """Trim and validate an account email while preserving submitted casing."""
    normalized = email.strip()
    if not normalized:
        raise ValueError("User email must not be blank")
    if len(normalized) > 254 or _EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("User email must be a valid email address")
    return normalized


def validate_password(password: str) -> str:
    """Reject an empty account password at the input boundary."""
    if not password:
        raise ValueError("Password must not be blank")
    return password


def validate_password_hash(password_hash: str) -> str:
    """Require the canonical Argon2id encoded hash representation."""
    normalized = password_hash.strip()
    parts = normalized.split("$")
    expected_prefix = ["", "argon2id", f"v={ARGON2_VERSION}", _ARGON2_PARAMETERS]
    if len(parts) != 6 or parts[:4] != expected_prefix:
        raise ValueError("Password hash must use the canonical Argon2id encoding")
    try:
        salt = _decode_argon2_component(parts[4])
        digest = _decode_argon2_component(parts[5])
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Password hash must use the canonical Argon2id encoding") from exc
    if len(salt) < 8 or len(digest) != _ARGON2_HASH_LEN:
        raise ValueError("Password hash must use the canonical Argon2id encoding")
    return normalized


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Create the canonical Argon2id password hash."""
    password_salt = salt or secrets.token_bytes(_ARGON2_SALT_LEN)
    return hash_secret(
        validate_password(password).encode("utf-8"),
        password_salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_ARGON2_HASH_LEN,
        type=Type.ID,
        version=ARGON2_VERSION,
    ).decode("ascii")


def _decode_argon2_component(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, validate=True)


def verify_password_hash(password: str, stored_hash: str) -> bool:
    """Verify a password against the canonical Argon2id representation."""
    expected = _INVALID_PASSWORD_DIGEST
    derived = _EMPTY_PASSWORD_DIGEST
    valid_format = False
    parts = stored_hash.split("$")
    expected_prefix = ["", "argon2id", f"v={ARGON2_VERSION}", _ARGON2_PARAMETERS]
    if len(parts) == 6 and parts[:4] == expected_prefix:
        try:
            salt = _decode_argon2_component(parts[4])
            candidate = _decode_argon2_component(parts[5])
        except (ValueError, binascii.Error):
            pass
        else:
            if len(salt) >= 8 and len(candidate) == _ARGON2_HASH_LEN:
                expected = candidate
                derived = hash_secret_raw(
                    password.encode("utf-8"),
                    salt,
                    time_cost=_ARGON2_TIME_COST,
                    memory_cost=_ARGON2_MEMORY_COST,
                    parallelism=_ARGON2_PARALLELISM,
                    hash_len=_ARGON2_HASH_LEN,
                    type=Type.ID,
                    version=ARGON2_VERSION,
                )
                valid_format = True

    password_matches = secrets.compare_digest(derived, expected)
    return valid_format and password_matches
