"""Grant presentation and principal identity binding for daemon routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from gobby.runtime_grants import (
    DeploymentGrantContext,
    GrantRejection,
    GrantRevocationStore,
    GrantService,
    RequiredCapability,
    StaleEpochGrant,
    decode_grant_header,
)
from gobby.runtime_grants.schema import BrokerOperation, GrantBundle, GrantPrincipal
from gobby.utils.local_token import AgentApiTokenClaims

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

GRANT_HEADER = "X-Gobby-Runtime-Grant"
MACHINE_HEADER = "X-Gobby-Machine-Id"
CALLER_PROJECT_HEADER = "X-Gobby-Caller-Project-Id"
TARGET_PROJECT_HEADER = "X-Gobby-Project-Id"
SESSION_HEADER = "X-Gobby-Session-Id"
AGENT_RUN_HEADER = "X-Gobby-Agent-Run-Id"
MANAGED_EXECUTION_HEADER = "X-Gobby-Managed-Execution-Id"


@dataclass(frozen=True, slots=True)
class GrantRoute:
    method: str
    route: str
    effectful: bool
    required: RequiredCapability | None


@dataclass(frozen=True, slots=True)
class AuthDecision:
    allowed: bool
    code: str | None = None
    message: str | None = None
    status_code: int = 401
    principal: GrantPrincipal | None = None
    grant: GrantBundle | None = None
    bearer_claims: AgentApiTokenClaims | None = None


_GRANT_ROUTES: tuple[GrantRoute, ...] = (
    GrantRoute(
        "POST",
        "/api/embeddings",
        True,
        RequiredCapability(name="embed", mode="daemon"),
    ),
    GrantRoute(
        "GET",
        "/api/embeddings/status",
        False,
        RequiredCapability(name="embed"),
    ),
    GrantRoute(
        "GET",
        "/api/embeddings/doctor",
        False,
        RequiredCapability(name="embed"),
    ),
    GrantRoute("GET", "/api/llm/status", False, RequiredCapability(name="text_generate")),
    GrantRoute(
        "POST",
        "/api/llm/generate",
        True,
        RequiredCapability(name="text_generate", mode="daemon"),
    ),
    GrantRoute(
        "POST",
        "/api/llm/chat/completions",
        True,
        RequiredCapability(name="tool_chat", mode="daemon"),
    ),
    GrantRoute("GET", "/api/llm/vision/status", False, RequiredCapability(name="vision_extract")),
    GrantRoute(
        "POST",
        "/api/llm/vision/extract",
        True,
        RequiredCapability(name="vision_extract", mode="daemon"),
    ),
    GrantRoute("GET", "/api/voice/status", False, RequiredCapability(name="audio_transcribe")),
    GrantRoute(
        "POST",
        "/api/voice/transcribe",
        True,
        RequiredCapability(name="audio_transcribe", mode="daemon"),
    ),
    GrantRoute("GET", "/api/wiki/code/status", False, None),
    GrantRoute(
        "POST",
        "/api/code-index/graph/clear",
        True,
        RequiredCapability(
            name="falkordb",
            operation=BrokerOperation(
                name="clear_projection",
                method="POST",
                path="/api/code-index/graph/clear",
            ),
        ),
    ),
    GrantRoute(
        "POST",
        "/api/code-index/graph/rebuild",
        True,
        RequiredCapability(
            name="falkordb",
            operation=BrokerOperation(
                name="rebuild_projection",
                method="POST",
                path="/api/code-index/graph/rebuild",
            ),
        ),
    ),
    GrantRoute(
        "POST",
        "/api/code-index/invalidate",
        True,
        RequiredCapability(
            name="qdrant",
            operation=BrokerOperation(
                name="invalidate_projection",
                method="POST",
                path="/api/code-index/invalidate",
            ),
        ),
    ),
    GrantRoute("POST", "/api/admin/savings/record", True, None),
)

_OPERATOR_EFFECTFUL_ROUTES = frozenset(
    {
        ("POST", "/api/code-index/prune"),
    }
)


def match_grant_route(method: str, path: str) -> GrantRoute | None:
    segments = path.strip("/").split("/")
    if not all(segments):
        return None
    method = method.upper()
    for entry in _GRANT_ROUTES:
        if entry.method != method:
            continue
        template = entry.route.strip("/").split("/")
        if len(template) != len(segments):
            continue
        if all(
            part == "*" or part == segment for part, segment in zip(template, segments, strict=True)
        ):
            return entry
    return None


def admission_required(method: str, path: str) -> bool:
    """Whether this request must hold the lease fence across handler execution."""
    route = match_grant_route(method, path)
    if route is not None:
        return route.effectful
    normalized = path.rstrip("/") or "/"
    return (method.upper(), normalized) in _OPERATOR_EFFECTFUL_ROUTES


def present_or_reject(
    grant_service: object,
    raw_header: str,
    *,
    now: int | None,
    required: RequiredCapability | None,
) -> AuthDecision | GrantBundle:
    try:
        grant = decode_grant_header(raw_header)
    except Exception as exc:
        return AuthDecision(
            allowed=False,
            code="invalid_signature",
            message=str(exc),
            status_code=401,
        )
    present = getattr(grant_service, "present", None)
    if not callable(present):
        return AuthDecision(allowed=False, code="missing_grant", status_code=401)
    try:
        return cast(GrantBundle, present(grant, now=now, required=required))
    except GrantRejection as exc:
        status = 409 if exc.code == "stale_epoch" else 401
        return AuthDecision(
            allowed=False,
            code=exc.code,
            message=exc.message,
            status_code=status,
        )


def bearer_matches_grant(
    principal: GrantPrincipal,
    *,
    claims: AgentApiTokenClaims | None,
    local_machine_id: str | None,
) -> bool:
    """Require the grant identity to match the bearer, or be an explicit narrowing.

    A live principal-A capability cannot present a copied principal-B grant.
    Grant fields may be omitted (narrower); they may not name a different owner.
    """
    if claims is None:
        return principal.kind == "interactive" and (
            local_machine_id is None or principal.machine_id == local_machine_id
        )
    if principal.project_id != claims.project_id:
        return False
    if principal.machine_id != claims.machine_id:
        return False
    if principal.session_id is not None and principal.session_id != claims.session_id:
        return False
    owner = claims.agent_run_id or claims.managed_execution_id
    if principal.execution_id is not None and principal.execution_id != owner:
        return False
    if claims.agent_run_id is not None:
        return principal.kind == "agent_run"
    if claims.managed_execution_id is not None:
        return principal.kind == "tool_chat"
    return False


def identity_headers_match(request: HTTPConnection, principal: GrantPrincipal) -> bool:
    headers = request.headers
    caller = _optional(headers.get(CALLER_PROJECT_HEADER))
    target = _optional(headers.get(TARGET_PROJECT_HEADER))
    session = _optional(headers.get(SESSION_HEADER))
    machine = _optional(headers.get(MACHINE_HEADER))
    run_id = _optional(headers.get(AGENT_RUN_HEADER))
    managed_id = _optional(headers.get(MANAGED_EXECUTION_HEADER))
    if caller is not None and caller != principal.project_id:
        return False
    if target is not None and target != principal.project_id:
        return False
    if session is not None and session != principal.session_id:
        return False
    if machine is not None and machine != principal.machine_id:
        return False
    if run_id is not None and run_id != principal.execution_id:
        return False
    if managed_id is not None and managed_id != principal.execution_id:
        return False
    return True


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class LiveLeaseGrantService:
    """Build a GrantService from the current lease epoch and signing secret."""

    def __init__(
        self,
        runtime: object,
        lease: object,
        clock: Callable[[], int],
        revocations: GrantRevocationStore | None = None,
    ) -> None:
        self._runtime = runtime
        self._lease = lease
        self._clock = clock
        self.revocations = revocations or GrantRevocationStore()

    def present(
        self,
        grant: GrantBundle,
        *,
        now: int | None = None,
        required: RequiredCapability | None = None,
    ) -> GrantBundle:
        return self._grant_service().present(grant, now=now, required=required)

    def revoke(self, grant: GrantBundle) -> None:
        self._grant_service().revoke(grant)

    def _grant_service(self) -> GrantService:
        epoch = getattr(self._lease, "fencing_epoch", None)
        secret = getattr(self._lease, "grant_signing_secret", None)
        token = getattr(self._lease, "deployment_token", None)
        if epoch is None or not secret or not token:
            raise StaleEpochGrant("active-daemon lease has no fencing epoch")
        return GrantService(
            runtime=self._runtime,  # type: ignore[arg-type]
            context=DeploymentGrantContext(
                token=str(token),
                fencing_epoch=int(epoch),
                signing_secret=str(secret),
            ),
            clock=self._clock,
            revocations=self.revocations,
        )
