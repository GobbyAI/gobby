"""Shared authentication service for daemon HTTP and WebSocket entry points."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple

from starlette.requests import HTTPConnection

from gobby.identity import DUMMY_PASSWORD_HASH, verify_password_hash
from gobby.servers.grant_auth import (
    GRANT_HEADER,
    AuthDecision,
    bearer_matches_grant,
    identity_headers_match,
    match_grant_route,
    present_or_reject,
)
from gobby.servers.lease_fence import EffectFence, LeaseNotHeld
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES
from gobby.storage.auth import (
    AuthStore,
    hash_token,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_resolution import resolve_session_reference
from gobby.storage.users import LocalUserManager, User
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    local_token_path,
    verify_agent_api_token,
)

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "gobby_session"
_LOCAL_TOKEN_HEADER = "X-Gobby-Local-Token"
_NEVER_REFRESHED = float("-inf")

_CALLER_PROJECT_HEADER = "X-Gobby-Caller-Project-Id"
_TARGET_PROJECT_HEADER = "X-Gobby-Project-Id"
_SESSION_HEADER = "X-Gobby-Session-Id"
_AGENT_RUN_HEADER = "X-Gobby-Agent-Run-Id"
_MANAGED_EXECUTION_HEADER = "X-Gobby-Managed-Execution-Id"
_HOOKS_EXECUTE_PATH = "/api/hooks/execute"


class _AgentRoute(NamedTuple):
    method: str
    route: str
    bind_identity: bool


# The single enumerated method+route capability matrix for run-scoped agent
# tokens. "*" matches exactly one path segment; every other segment matches
# exactly. bind_identity marks context-bearing routes whose identity is
# mandatory. AI and broker rows also require a presented runtime grant;
# identity is taken from that grant principal, not from caller headers.
# Operator mutation and configuration routes stay excluded.
_AGENT_CAPABILITY_MATRIX: tuple[_AgentRoute, ...] = (
    # MCP proxy discovery and execution.
    _AgentRoute("GET", "/api/mcp/servers", True),
    _AgentRoute("GET", "/api/mcp/status", True),
    _AgentRoute("GET", "/api/mcp/tools", True),
    _AgentRoute("GET", "/api/mcp/*/tools", True),
    _AgentRoute("POST", "/api/mcp/tools/schema", True),
    _AgentRoute("POST", "/api/mcp/tools/call", True),
    _AgentRoute("POST", "/api/mcp/tools/recommend", True),
    _AgentRoute("POST", "/api/mcp/tools/search", True),
    _AgentRoute("POST", "/api/mcp/*/tools/*", True),
    # Session-scoped workflow variables (stdio proxy get/set_variable).
    _AgentRoute("POST", "/api/workflows/variables/get", True),
    _AgentRoute("POST", "/api/workflows/variables/set", True),
    # Hook execution (ghook).
    _AgentRoute("POST", _HOOKS_EXECUTE_PATH, True),
    # Dormant wiki code status and code-index graph lifecycle.
    _AgentRoute("GET", "/api/wiki/code/status", True),
    _AgentRoute("POST", "/api/code-index/graph/clear", True),
    _AgentRoute("POST", "/api/code-index/graph/rebuild", True),
    _AgentRoute("POST", "/api/code-index/invalidate", True),
    # AI compute and capability probes (gcore-backed binaries).
    _AgentRoute("POST", "/api/embeddings", True),
    _AgentRoute("GET", "/api/embeddings/status", True),
    _AgentRoute("GET", "/api/embeddings/doctor", True),
    _AgentRoute("GET", "/api/llm/status", True),
    _AgentRoute("POST", "/api/llm/generate", True),
    _AgentRoute("POST", "/api/llm/chat/completions", True),
    _AgentRoute("GET", "/api/llm/vision/status", True),
    _AgentRoute("POST", "/api/llm/vision/extract", True),
    _AgentRoute("GET", "/api/voice/status", True),
    _AgentRoute("POST", "/api/voice/transcribe", True),
    _AgentRoute("GET", "/api/providers/models", False),
    _AgentRoute("POST", "/api/runtime/handshake", True),
    _AgentRoute("GET", "/api/runtime/config", True),
    # Read-only `gobby` CLI listings backed by DaemonClient.
    _AgentRoute("GET", "/api/comms/channels", False),
    _AgentRoute("GET", "/api/webhooks", False),
    _AgentRoute("GET", "/api/embeddings/switch/status", False),
    _AgentRoute("GET", "/api/memories/graph/counts", False),
    _AgentRoute("GET", "/api/memories/graph/rebuild/status", False),
)


def _agent_capability_allows(request: HTTPConnection) -> _AgentRoute | None:
    """Match a request against the agent capability matrix.

    Returns the matched entry, or None when the route is outside agent
    capability. The matrix above is the single source of agent capability.
    """
    method = str(request.scope.get("method", "GET")).upper()
    segments = request.url.path.strip("/").split("/")
    if not all(segments):
        return None
    for entry in _AGENT_CAPABILITY_MATRIX:
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


def _agent_identity_matches(
    request: HTTPConnection,
    claims: AgentApiTokenClaims,
    *,
    bind_identity: bool,
    resolve_session: Callable[[str], str | None],
) -> bool:
    headers = request.headers
    caller_project = headers.get(_CALLER_PROJECT_HEADER) or headers.get(_TARGET_PROJECT_HEADER)
    session_ref = headers.get(_SESSION_HEADER)
    run_id = headers.get(_AGENT_RUN_HEADER)
    managed_execution_id = headers.get(_MANAGED_EXECUTION_HEADER)
    if bind_identity and (caller_project is None or session_ref is None):
        return False
    if caller_project is not None and caller_project != claims.project_id:
        return False
    if session_ref is not None and session_ref != claims.session_id:
        # Refs like "#42" or UUID prefixes are legal caller spellings of the
        # claimed session; compare canonical UUIDs, never raw refs.
        if resolve_session(session_ref) != claims.session_id:
            return False
    expected_owner = claims.agent_run_id or claims.managed_execution_id
    supplied_owner = run_id or managed_execution_id
    if run_id is not None and claims.agent_run_id is None:
        return False
    if managed_execution_id is not None and claims.managed_execution_id is None:
        return False
    if supplied_owner is not None:
        return supplied_owner == expected_owner
    return not bind_identity


def _read_token_file(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Unable to read local API token file %s: %s", path, exc)
        return None
    return token or None


class AuthService:
    """Cache and verify all daemon authentication credentials."""

    MIN_REFRESH_INTERVAL = 5.0

    def __init__(
        self,
        database_getter: Callable[[], HubDatabase],
        token_file: Path | None = None,
        *,
        grant_service: object | None = None,
        lease_live: Callable[[], bool] | None = None,
        local_machine_id: str | None = None,
        effect_fence: EffectFence | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._database_getter = database_getter
        self._token_file = token_file or local_token_path()
        self._lock = threading.Lock()
        self._last_refresh = _NEVER_REFRESHED
        self._token_hash: str | None = None
        self._local_token_plaintext: str | None = None
        self._grant_service = grant_service
        self._lease_live = lease_live
        self._local_machine_id = local_machine_id
        self._effect_fence = effect_fence
        self._clock = clock

    def bind_runtime(
        self,
        *,
        grant_service: object | None,
        lease_live: Callable[[], bool] | None,
        local_machine_id: str | None,
        effect_fence: EffectFence | None,
        clock: Callable[[], int] | None,
    ) -> None:
        self._grant_service = grant_service
        self._lease_live = lease_live
        self._local_machine_id = local_machine_id
        self._effect_fence = effect_fence
        self._clock = clock

    def verify_bearer(self, token: str) -> bool:
        candidate_hash = hash_token(token)
        self.refresh()

        if secrets.compare_digest(candidate_hash, self._token_hash_snapshot()):
            return True

        self.refresh()
        return secrets.compare_digest(candidate_hash, self._token_hash_snapshot())

    async def verify_ws_token(self, token: str) -> str | None:
        return "local-cli" if self.verify_bearer(token) else None

    def is_request_authenticated(self, request: HTTPConnection) -> bool:
        return self.authenticate(request).allowed

    def authenticate(self, request: HTTPConnection) -> AuthDecision:
        grant_route = match_grant_route(
            str(request.scope.get("method", "GET")),
            request.url.path,
        )
        if grant_route is None:
            return AuthDecision(allowed=self._legacy_authenticated(request))

        bearer = self._accepted_bearer(request)
        if bearer is False:
            return AuthDecision(allowed=False, code="missing_auth", status_code=401)

        raw_grant = request.headers.get(GRANT_HEADER)
        if raw_grant is None or not raw_grant.strip():
            return AuthDecision(allowed=False, code="missing_grant", status_code=401)
        if self._grant_service is None:
            return AuthDecision(allowed=False, code="missing_grant", status_code=401)

        now = self._clock() if self._clock is not None else None
        presented = present_or_reject(
            self._grant_service,
            raw_grant,
            now=now,
            required=None,
        )
        if isinstance(presented, AuthDecision):
            return presented
        if not bearer_matches_grant(
            presented.principal,
            claims=bearer,
            local_machine_id=self._local_machine_id,
        ):
            return AuthDecision(allowed=False, code="forged_identity", status_code=401)
        if not identity_headers_match(request, presented.principal):
            return AuthDecision(allowed=False, code="forged_identity", status_code=401)
        if grant_route.required is not None:
            presented = present_or_reject(
                self._grant_service,
                raw_grant,
                now=now,
                required=grant_route.required,
            )
            if isinstance(presented, AuthDecision):
                return presented
        if grant_route.effectful and not self._effectful_allowed():
            return AuthDecision(allowed=False, code="lease_not_held", status_code=409)
        return AuthDecision(
            allowed=True,
            principal=presented.principal,
            grant=presented,
            bearer_claims=bearer,
            status_code=200,
        )

    def _legacy_authenticated(self, request: HTTPConnection) -> bool:
        authorization = request.headers.get("Authorization")
        if authorization is not None:
            parts = authorization.split(maxsplit=1)
            if parts and parts[0].casefold() == "bearer":
                if len(parts) != 2:
                    return False
                return self.verify_bearer(parts[1]) or self._verify_agent_request(request, parts[1])

        local_token = request.headers.get(_LOCAL_TOKEN_HEADER)
        if local_token is not None:
            return self.verify_bearer(local_token)

        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None:
            return self.validate_session(session_token)

        return False

    def _credential_accepted(self, request: HTTPConnection) -> bool:
        return self._accepted_bearer(request) is not False

    def _accepted_bearer(
        self, request: HTTPConnection
    ) -> AgentApiTokenClaims | None | Literal[False]:
        """Return agent claims, None for the local operator, or False if rejected."""
        authorization = request.headers.get("Authorization")
        if authorization is not None:
            parts = authorization.split(maxsplit=1)
            if parts and parts[0].casefold() == "bearer" and len(parts) == 2:
                if self.verify_bearer(parts[1]):
                    return None
                claims = self._verified_agent_claims_for_token(request, parts[1])
                return claims if claims is not None else False
        local_token = request.headers.get(_LOCAL_TOKEN_HEADER)
        if local_token is not None:
            return None if self.verify_bearer(local_token) else False
        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None:
            return None if self.validate_session(session_token) else False
        return False

    def _agent_credential_accepted(self, request: HTTPConnection, token: str) -> bool:
        return self._verified_agent_claims_for_token(request, token) is not None

    def _effectful_allowed(self) -> bool:
        if self._lease_live is not None and not self._lease_live():
            return False
        fence = self._effect_fence
        if fence is None:
            return True
        try:
            with fence.admit():
                return True
        except LeaseNotHeld:
            return False

    def verified_agent_claims(self, request: HTTPConnection) -> AgentApiTokenClaims | None:
        """Return identity claims only for a valid run-scoped agent request."""
        authorization = request.headers.get("Authorization")
        if authorization is None:
            return None
        parts = authorization.split(maxsplit=1)
        if len(parts) != 2 or parts[0].casefold() != "bearer":
            return None
        return self._verified_agent_claims_for_token(request, parts[1])

    def _verify_agent_request(self, request: HTTPConnection, token: str) -> bool:
        return self._verified_agent_claims_for_token(request, token) is not None

    def _verified_agent_claims_for_token(
        self,
        request: HTTPConnection,
        token: str,
    ) -> AgentApiTokenClaims | None:
        operator_token = self.local_token()
        if operator_token is None:
            return None
        claims = verify_agent_api_token(token, operator_token)
        if claims is None:
            return None
        entry = _agent_capability_allows(request)
        if entry is None:
            return None
        if not self._managed_capability_is_live(claims):
            return None
        if not _agent_identity_matches(
            request,
            claims,
            bind_identity=entry.bind_identity,
            resolve_session=lambda ref: self._resolve_agent_session_ref(ref, claims.project_id),
        ):
            return None
        return claims

    def _managed_capability_is_live(self, claims: AgentApiTokenClaims) -> bool:
        if claims.agent_run_id is not None:
            return self._agent_run_is_live(claims.agent_run_id)
        if claims.managed_execution_id is None:
            return False
        try:
            row = self._database_getter().fetchone(
                "SELECT gobby_agent_auth.managed_execution_is_login_capable(%s) AS login_capable",
                (claims.managed_execution_id,),
            )
        except Exception:
            return False
        return row is not None and row["login_capable"] is True

    def _agent_run_is_live(self, run_id: str) -> bool:
        """A capability dies with its run: only pending/running runs pass.

        Checked on every request; token expiry is only defense-in-depth
        around this, the real revocation.
        """
        try:
            row = self._database_getter().fetchone(
                "SELECT status FROM agent_runs WHERE id = %s",
                (run_id,),
            )
        except Exception:
            # Malformed run ids and storage failures must fail closed at the
            # auth boundary, never surface as a 500.
            return False
        return row is not None and row["status"] in ACTIVE_AGENT_RUN_STATUSES

    def _resolve_agent_session_ref(self, ref: str, project_id: str) -> str | None:
        try:
            return resolve_session_reference(self._database_getter(), ref, project_id)
        except Exception:
            # Unresolvable or malformed refs must fail closed at the auth
            # boundary, never surface as a 500.
            return None

    def validate_session(self, token: str) -> bool:
        if not token:
            return False
        return AuthStore(self._database_getter()).validate_session(token)

    def verify_password(self, email: str, password: str) -> User | None:
        try:
            user = LocalUserManager(self._database_getter()).get_by_email(email)
        except ValueError:
            user = None
        stored_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
        if not verify_password_hash(password, stored_hash):
            return None
        return user

    def local_token(self) -> str | None:
        self.refresh()
        with self._lock:
            return self._local_token_plaintext

    def refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_refresh < self.MIN_REFRESH_INTERVAL:
                return

            token_hash = AuthStore(self._database_getter()).get_local_api_token_hash()
            local_token_plaintext = _read_token_file(self._token_file)

            self._token_hash = token_hash
            self._local_token_plaintext = local_token_plaintext
            self._last_refresh = now

    def _token_hash_snapshot(self) -> str:
        with self._lock:
            return self._token_hash or ""
