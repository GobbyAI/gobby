"""v2 runtime grant bundle models."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

API_CONTRACT = 1
GRANT_VERSION = 2


class BrokerOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    method: Literal["GET", "POST", "PUT", "DELETE"]
    path: str


class PostgresDirect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["direct"] = "direct"
    dsn: str
    role_name: str
    credential_generation: int
    valid_until: int


class FalkorDirect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["direct"] = "direct"
    host: str
    port: int
    password: str


class QdrantDirect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["direct"] = "direct"
    url: str
    api_key: str


class BrokeredCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["brokered"] = "brokered"
    operations: tuple[BrokerOperation, ...]


class UnavailableCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["unavailable"] = "unavailable"


class AIDaemonCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["daemon"] = "daemon"


class AIUnavailableCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["unavailable"] = "unavailable"


PostgresCapability = Annotated[
    PostgresDirect | BrokeredCapability | UnavailableCapability,
    Field(discriminator="mode"),
]
FalkorCapability = Annotated[
    FalkorDirect | BrokeredCapability | UnavailableCapability,
    Field(discriminator="mode"),
]
QdrantCapability = Annotated[
    QdrantDirect | BrokeredCapability | UnavailableCapability,
    Field(discriminator="mode"),
]
AICapability = Annotated[
    AIDaemonCapability | AIUnavailableCapability,
    Field(discriminator="mode"),
]


class GrantDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(pattern=r"^[0-9a-f]{16}$")
    fencing_epoch: int


class SchemaIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runner_protocol: int
    baseline_version: int
    baseline_checksum: str
    latest_version: int
    latest_checksum: str
    assets_root_hash: str


class GrantPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["interactive", "agent_run", "tool_chat", "maintenance"]
    machine_id: str
    project_id: str
    execution_id: str | None
    session_id: str | None


class GrantCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    postgres: PostgresCapability
    falkordb: FalkorCapability
    qdrant: QdrantCapability
    embed: AICapability
    text_generate: AICapability
    tool_chat: AICapability
    vision_extract: AICapability
    audio_transcribe: AICapability
    broker_operations: tuple[BrokerOperation, ...]


class GrantBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = GRANT_VERSION
    api_contract: int = API_CONTRACT
    config_revision: int
    deployment: GrantDeployment
    schema_identity: SchemaIdentity
    principal: GrantPrincipal
    capabilities: GrantCapabilities
    issued_at: int
    expires_at: int
    payload_checksum: str = ""
    signature: str = ""

    def model_dump_canonical(self) -> bytes:
        """Serialize the full wire grant as sorted compact JSON with a trailing newline."""
        return (
            json.dumps(self.model_dump(mode="json"), separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
