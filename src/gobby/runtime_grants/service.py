"""Issue and reject v2 grants against one captured configuration revision."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.runtime_grants.revocation import GrantRevocationStore
from gobby.runtime_grants.schema import (
    API_CONTRACT,
    GRANT_VERSION,
    AIDaemonCapability,
    AIUnavailableCapability,
    BrokeredCapability,
    BrokerOperation,
    FalkorDirect,
    GrantBundle,
    GrantCapabilities,
    GrantDeployment,
    GrantPrincipal,
    PostgresCapability,
    QdrantDirect,
    SchemaIdentity,
    UnavailableCapability,
)
from gobby.runtime_grants.signing import payload_checksum, sign_grant, signature_matches
from gobby.storage.schema_contract import expected_schema_identity

EMBED_OPERATION = BrokerOperation(name="embed", method="POST", path="/api/embeddings")
FALKOR_OPERATIONS = (
    BrokerOperation(name="clear_projection", method="POST", path="/api/code-index/graph/clear"),
    BrokerOperation(name="rebuild_projection", method="POST", path="/api/code-index/graph/rebuild"),
)
QDRANT_OPERATIONS = (
    BrokerOperation(name="invalidate_projection", method="POST", path="/api/code-index/invalidate"),
)


class ConfigCapture(Protocol):
    def capture(self) -> RuntimeActiveBundle: ...


@dataclass(frozen=True, slots=True)
class DeploymentGrantContext:
    token: str
    fencing_epoch: int
    signing_secret: str


@dataclass(frozen=True, slots=True)
class RequiredCapability:
    name: str
    mode: str | None = None
    operation: BrokerOperation | None = None


class GrantRejection(Exception):
    code: str = "grant_rejected"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ExpiredGrant(GrantRejection):
    code = "expired"


class InvalidGrantSignature(GrantRejection):
    code = "invalid_signature"


class WrongDeploymentGrant(GrantRejection):
    code = "wrong_deployment"


class WrongSchemaGrant(GrantRejection):
    code = "wrong_schema"


class WrongApiContractGrant(GrantRejection):
    code = "wrong_api_contract"


class WrongCapabilityGrant(GrantRejection):
    code = "wrong_capability"


class StaleEpochGrant(GrantRejection):
    code = "stale_epoch"


class RevokedGrant(GrantRejection):
    code = "revoked"


def _unresolved(value: object) -> bool:
    return isinstance(value, str) and ("$secret:" in value or "${" in value)


def _url_requires_broker(value: str | None) -> bool:
    if not value or _unresolved(value):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    )


def _falkor_capability(
    snapshot: ConfigSnapshot,
) -> FalkorDirect | BrokeredCapability | UnavailableCapability:
    config = snapshot.active.databases.falkordb
    host = config.host
    password = snapshot.active_secret("databases.falkordb.password")
    if password is None and config.password and not _unresolved(config.password):
        password = config.password
    if _unresolved(host) or not host:
        return UnavailableCapability()
    if password:
        return FalkorDirect(host=host, port=config.port, password=password)
    return BrokeredCapability(operations=FALKOR_OPERATIONS)


def _qdrant_capability(
    snapshot: ConfigSnapshot,
) -> QdrantDirect | BrokeredCapability | UnavailableCapability:
    config = snapshot.active.databases.qdrant
    url = config.url
    api_key = snapshot.active_secret("databases.qdrant.api_key")
    if api_key is None and config.api_key and not _unresolved(config.api_key):
        api_key = config.api_key
    if _url_requires_broker(url):
        return BrokeredCapability(operations=QDRANT_OPERATIONS) if url else UnavailableCapability()
    if api_key and url:
        return QdrantDirect(url=url, api_key=api_key)
    return BrokeredCapability(operations=QDRANT_OPERATIONS)


def _ai_capability(enabled: bool) -> AIDaemonCapability | AIUnavailableCapability:
    return AIDaemonCapability() if enabled else AIUnavailableCapability()


def capabilities_from_snapshot(
    snapshot: ConfigSnapshot,
    postgres: PostgresCapability,
) -> GrantCapabilities:
    falkordb = _falkor_capability(snapshot)
    qdrant = _qdrant_capability(snapshot)
    embed_enabled = bool(snapshot.active.embeddings.model) and not _unresolved(
        snapshot.active.embeddings.model
    )
    embed = _ai_capability(embed_enabled)
    operations: list[BrokerOperation] = []
    if embed.mode == "daemon":
        operations.append(EMBED_OPERATION)
    if falkordb.mode == "brokered":
        operations.extend(falkordb.operations)
    if qdrant.mode == "brokered":
        operations.extend(qdrant.operations)
    return GrantCapabilities(
        postgres=postgres,
        falkordb=falkordb,
        qdrant=qdrant,
        embed=embed,
        text_generate=AIDaemonCapability(),
        tool_chat=AIDaemonCapability(),
        vision_extract=AIUnavailableCapability(),
        audio_transcribe=AIUnavailableCapability(),
        broker_operations=tuple(operations),
    )


def _schema_identity() -> SchemaIdentity:
    return SchemaIdentity.model_validate(expected_schema_identity())


def _capability_matches(grant: GrantBundle, required: RequiredCapability) -> bool:
    capability = getattr(grant.capabilities, required.name, None)
    if capability is None:
        return False
    mode = getattr(capability, "mode", None)
    if required.mode is not None and mode != required.mode:
        return False
    if required.operation is None:
        return mode != "unavailable"
    if mode == "unavailable":
        return False
    if mode == "brokered":
        return required.operation in capability.operations
    return True


@dataclass
class GrantService:
    runtime: ConfigCapture
    context: DeploymentGrantContext
    clock: Callable[[], int] | None = None
    revocations: GrantRevocationStore = field(default_factory=GrantRevocationStore)

    def issue(
        self,
        *,
        principal: GrantPrincipal,
        postgres: PostgresCapability,
        now: int | None = None,
        ttl_seconds: int,
    ) -> GrantBundle:
        bundle = self.runtime.capture()
        snapshot = bundle.snapshot
        issued_at = self._now(now)
        unsigned = GrantBundle(
            version=GRANT_VERSION,
            api_contract=API_CONTRACT,
            config_revision=snapshot.revision,
            deployment=GrantDeployment(
                token=self.context.token,
                fencing_epoch=self.context.fencing_epoch,
            ),
            schema_identity=_schema_identity(),
            principal=principal,
            capabilities=capabilities_from_snapshot(snapshot, postgres),
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
        )
        return sign_grant(unsigned, self.context.signing_secret)

    def present(
        self,
        grant: GrantBundle,
        *,
        now: int | None = None,
        required: RequiredCapability | None = None,
    ) -> GrantBundle:
        if not signature_matches(grant, self.context.signing_secret):
            raise InvalidGrantSignature("grant signature is invalid")
        if not hmac.compare_digest(grant.payload_checksum, payload_checksum(grant)):
            raise InvalidGrantSignature("grant payload checksum is invalid")
        if grant.deployment.token != self.context.token:
            raise WrongDeploymentGrant("grant deployment token does not match")
        if grant.schema_identity != _schema_identity():
            raise WrongSchemaGrant("grant schema identity does not match")
        if grant.api_contract != API_CONTRACT or grant.version != GRANT_VERSION:
            raise WrongApiContractGrant("grant API contract is not supported")
        if grant.deployment.fencing_epoch != self.context.fencing_epoch:
            raise StaleEpochGrant("grant fencing epoch is stale")
        if self._now(now) >= grant.expires_at:
            raise ExpiredGrant("grant has expired")
        if self.revocations.is_revoked(grant):
            raise RevokedGrant("grant has been revoked")
        if required is not None and not _capability_matches(grant, required):
            raise WrongCapabilityGrant("grant lacks the required capability")
        return grant

    def revoke(self, grant: GrantBundle) -> None:
        self.revocations.revoke_grant(grant)

    def _now(self, now: int | None) -> int:
        if now is not None:
            return now
        if self.clock is not None:
            return self.clock()
        raise RuntimeError("GrantService requires an explicit now or clock")
