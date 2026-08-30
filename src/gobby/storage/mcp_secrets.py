"""Secure persistence helpers for MCP environment variables and headers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from gobby.storage.secret_names import SECRET_REF_PATTERN
from gobby.storage.secrets import SecretStore

SECRET_REF_PREFIX = "$secret:"
_MANAGED_DESCRIPTION_PREFIX = "Gobby-managed MCP secret:"
_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SAFE_NAME_RE = re.compile(r"[^a-z0-9]+")
_HIGH_CONFIDENCE_VALUE_RE = re.compile(
    r"^(?:"
    r"(?:bearer|basic)\s+\S+"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{12,}"
    r"|gh[pousr]_[A-Za-z0-9_]{12,}"
    r"|github_pat_[A-Za-z0-9_]{12,}"
    r"|xox[baprs]-[A-Za-z0-9-]{12,}"
    r")$",
    re.IGNORECASE,
)
_SECRET_TOKENS = {
    "auth",
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "passwd",
    "password",
    "secret",
    "token",
}
_SECRET_TOKEN_PAIRS = {
    ("api", "key"),
    ("client", "key"),
    ("private", "key"),
    ("signing", "key"),
}


@dataclass(frozen=True, slots=True)
class MCPSecretSlot:
    """Stable identity and ownership marker for one persisted MCP value."""

    persistence: str
    scope: str
    server_name: str
    field: str
    key: str

    @property
    def identity(self) -> str:
        return "\0".join(
            (self.persistence, self.scope, self.server_name.lower(), self.field, self.key.lower())
        )

    @property
    def name(self) -> str:
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()[:16]
        server = _safe_component(self.server_name, fallback="server")[:24]
        key = _safe_component(self.key, fallback="value")[:24]
        return f"mcp_{server}_{self.field}_{key}_{digest}"

    @property
    def description(self) -> str:
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()
        return f"{_MANAGED_DESCRIPTION_PREFIX}{digest}"


def _safe_component(value: str, *, fallback: str) -> str:
    normalized = _SAFE_NAME_RE.sub("_", value.strip().lower()).strip("_")
    return normalized or fallback


def is_explicit_secret_reference(value: str) -> bool:
    """Return whether a value already delegates any content to SecretStore."""
    return SECRET_REF_PATTERN.search(value) is not None


def is_secret_looking_mcp_value(key: str, value: str) -> bool:
    """Classify high-confidence credential slots without broad entropy guessing."""
    tokens = _NAME_TOKEN_RE.findall(key.lower())
    token_set = set(tokens)
    if token_set & _SECRET_TOKENS:
        return True
    if "apikey" in token_set:
        return True
    if any(pair in zip(tokens, tokens[1:], strict=False) for pair in _SECRET_TOKEN_PAIRS):
        return True
    return _HIGH_CONFIDENCE_VALUE_RE.fullmatch(value.strip()) is not None


def protect_mcp_mapping(
    values: dict[str, str] | None,
    *,
    secret_store: SecretStore | None,
    persistence: str,
    scope: str,
    server_name: str,
    field: str,
) -> dict[str, str] | None:
    """Replace credential plaintext with deterministic encrypted-secret references."""
    if values is None:
        return None

    protected: dict[str, str] = {}
    for key, value in values.items():
        if is_explicit_secret_reference(value) or not is_secret_looking_mcp_value(key, value):
            protected[key] = value
            continue
        if secret_store is None:
            raise ValueError(f"MCP {field} value {key!r} requires SecretStore-backed persistence")
        slot = MCPSecretSlot(persistence, scope, server_name, field, key)
        secret_store.set(
            slot.name,
            value,
            category="mcp_server",
            description=slot.description,
            project_id=scope,
        )
        protected[key] = f"{SECRET_REF_PREFIX}{slot.name}"
    return protected


def cleanup_replaced_mcp_secrets(
    secret_store: SecretStore,
    *,
    persistence: str,
    scope: str,
    server_name: str,
    old_env: dict[str, str] | None,
    old_headers: dict[str, str] | None,
    new_env: dict[str, str] | None,
    new_headers: dict[str, str] | None,
) -> None:
    """Delete managed secrets whose owning MCP slot no longer references them."""
    new_references = secret_store.find_persisted_secret_references((new_env, new_headers))
    for field, values in (("env", old_env), ("headers", old_headers)):
        for key, value in (values or {}).items():
            slot = MCPSecretSlot(persistence, scope, server_name, field, key)
            if value != f"{SECRET_REF_PREFIX}{slot.name}" or slot.name in new_references:
                continue
            row = secret_store.db.fetchone(
                "SELECT description FROM secrets WHERE name = %s AND project_id = %s",
                (slot.name, scope),
            )
            if row is not None and row["description"] == slot.description:
                secret_store.delete(slot.name, project_id=scope)
