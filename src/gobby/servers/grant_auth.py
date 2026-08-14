"""Grant presentation and principal identity binding for daemon routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from gobby.runtime_grants import (
    DeploymentGrantContext,
    GrantRejection,
    GrantService,
    RequiredCapability,
    StaleEpochGrant,
    decode_grant_header,
)
from gobby.runtime_grants.schema import BrokerOperation, GrantBundle, GrantPrincipal

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
    ) -> None:
        self._runtime = runtime
        self._lease = lease
        self._clock = clock

    def present(
        self,
        grant: GrantBundle,
        *,
        now: int | None = None,
        required: RequiredCapability | None = None,
    ) -> GrantBundle:
        epoch = getattr(self._lease, "fencing_epoch", None)
        secret = getattr(self._lease, "grant_signing_secret", None)
        token = getattr(self._lease, "deployment_token", None)
        if epoch is None or not secret or not token:
            raise StaleEpochGrant("active-daemon lease has no fencing epoch")
        service = GrantService(
            runtime=self._runtime,  # type: ignore[arg-type]
            context=DeploymentGrantContext(
                token=str(token),
                fencing_epoch=int(epoch),
                signing_secret=str(secret),
            ),
            clock=self._clock,
        )
        return service.present(grant, now=now, required=required)
