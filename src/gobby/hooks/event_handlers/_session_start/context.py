"""Session-start context injection mode helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from gobby.storage.sessions.startup_claim import StartupContextClaim

ContextInjectionMode = Literal["full", "live"]

_CONTEXT_LOSS_SOURCES = {"clear", "compact"}
_CLAIM_METADATA_KEY = "_session_start_context_claim"


@dataclass(frozen=True)
class SessionStartContextDecision:
    """Classified context injection mode for a SessionStart event.

    ``claim`` is the claimed generation this worker owns, when the decision
    allocated (or adopted) one. ``owner_external`` marks a claim whose owner
    token came from a pre-submission preflight: its commit is a delivery
    receipt effect, never an eager post-emit commit.
    """

    mode: ContextInjectionMode
    variables: dict[str, Any]
    explicit_context_loss: bool
    claim: StartupContextClaim | None = None
    owner_external: bool = False

    @property
    def commits_on_emit(self) -> bool:
        """Whether emitting full context should commit the claim inline."""
        return (
            self.claim is not None
            and self.claim.owner_token is not None
            and not self.owner_external
        )


def startup_claim_owner_token(event: Any) -> str | None:
    """Return the private preflight owner token carried on a hook event."""
    metadata = getattr(event, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    claim = metadata.get("_gobby_startup_claim")
    if not isinstance(claim, dict):
        return None
    token = claim.get("owner_token")
    if isinstance(token, str) and token:
        return token
    return None


def classify_session_start_context(
    handler: Any,
    *,
    session_id: str | None,
    session: Any | None,
    session_source: str | None,
    is_existing_session: bool,
    owner_token: str | None = None,
) -> SessionStartContextDecision:
    """Decide whether SessionStart should emit full startup or live context.

    Startup context is inferred from persisted session evidence: the durable
    claim generation on the sessions row, first-prompt agent injection, and
    recorded activity. ``is_existing_session`` is informational only.
    """
    del is_existing_session
    variables = _load_session_variables(handler, session_id)
    explicit_context_loss = _has_explicit_context_loss(session_source, variables)
    owner_external = owner_token is not None

    if explicit_context_loss:
        # Full context is forced; still allocate the generation so the emit
        # leaves durable evidence on rows that never committed one.
        owned = _claim_startup_context_atomically(handler, session_id, owner_token=owner_token)
        return SessionStartContextDecision(
            "full",
            variables,
            explicit_context_loss,
            claim=_owned_claim(owned),
            owner_external=owner_external,
        )

    if _has_prior_context_evidence(session, variables):
        return SessionStartContextDecision("live", variables, explicit_context_loss)

    if session_id:
        claimed = _claim_startup_context_atomically(handler, session_id, owner_token=owner_token)
        mode: ContextInjectionMode = "full" if claimed is None else claimed.mode
        return SessionStartContextDecision(
            mode,
            variables,
            explicit_context_loss,
            claim=_owned_claim(claimed),
            owner_external=owner_external,
        )

    return SessionStartContextDecision("full", variables, explicit_context_loss)


def _owned_claim(claim: StartupContextClaim | None) -> StartupContextClaim | None:
    if claim is None or claim.mode != "full" or claim.state != "claimed":
        return None
    if not isinstance(claim.generation, int) or not isinstance(claim.owner_token, str):
        return None
    return claim


def _claim_startup_context_atomically(
    handler: Any,
    session_id: str | None,
    owner_token: str | None = None,
) -> StartupContextClaim | None:
    """Atomically claim startup context for this session.

    Returns the claim result, or ``None`` when no durable claim could be made
    (callers then emit full context without a commit).
    """
    if not session_id or not getattr(handler, "_session_manager", None):
        return None

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(handler._session_manager.db)
        if owner_token:
            result = sv_mgr.claim_startup_context(session_id, owner_token=owner_token)
        else:
            result = sv_mgr.claim_startup_context(session_id)
    except Exception as e:
        handler.logger.debug("Failed to claim startup context for %s: %s", session_id, e)
        return None
    return _coerce_claim(result)


def _coerce_claim(result: Any) -> StartupContextClaim | None:
    if isinstance(result, StartupContextClaim):
        return result
    mode = getattr(result, "mode", None)
    if mode not in {"full", "live"}:
        return None
    generation = getattr(result, "generation", None)
    owner_token = getattr(result, "owner_token", None)
    state = getattr(result, "state", None)
    return StartupContextClaim(
        mode,
        generation if isinstance(generation, int) else 0,
        owner_token if isinstance(owner_token, str) else None,
        state if state in {"idle", "claimed", "committed", "invalidated"} else "idle",
    )


def commit_startup_context_claim(
    handler: Any,
    session_id: str | None,
    decision: SessionStartContextDecision,
) -> bool:
    """Commit the emitted full-context claim (non-receipt providers only)."""
    claim = _emit_committable_claim(decision)
    if not session_id or claim is None:
        return False
    return _commit_claim(handler, session_id, claim.generation, claim.owner_token)


def _emit_committable_claim(decision: Any) -> StartupContextClaim | None:
    """Return the claim an emit may commit; tolerates duck-typed decisions."""
    if not getattr(decision, "commits_on_emit", False):
        return None
    claim = getattr(decision, "claim", None)
    return claim if isinstance(claim, StartupContextClaim) else None


def stash_startup_claim(metadata: dict[str, Any], decision: SessionStartContextDecision) -> None:
    """Carry an emit-committable claim across the materialize/compose seam."""
    metadata.pop(_CLAIM_METADATA_KEY, None)
    claim = _emit_committable_claim(decision)
    if claim is not None:
        metadata[_CLAIM_METADATA_KEY] = {
            "generation": claim.generation,
            "owner_token": claim.owner_token,
        }


def commit_stashed_startup_claim(
    handler: Any,
    session_id: str | None,
    metadata: dict[str, Any],
) -> bool:
    """Commit a claim stashed by :func:`stash_startup_claim`, consuming it."""
    raw = metadata.pop(_CLAIM_METADATA_KEY, None)
    if not session_id or not isinstance(raw, dict):
        return False
    generation = raw.get("generation")
    owner_token = raw.get("owner_token")
    if not isinstance(generation, int) or not isinstance(owner_token, str) or not owner_token:
        return False
    return _commit_claim(handler, session_id, generation, owner_token)


def _commit_claim(
    handler: Any,
    session_id: str,
    generation: int,
    owner_token: str | None,
) -> bool:
    if owner_token is None or not getattr(handler, "_session_manager", None):
        return False
    try:
        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(handler._session_manager.db)
        return bool(sv_mgr.commit_startup_context(session_id, generation, owner_token))
    except Exception as e:
        handler.logger.debug("Failed to commit startup context for %s: %s", session_id, e)
        return False


def _load_session_variables(handler: Any, session_id: str | None) -> dict[str, Any]:
    if not session_id or not getattr(handler, "_session_manager", None):
        return {}

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        variables = SessionVariableManager(handler._session_manager.db).get_variables(session_id)
    except Exception as e:
        handler.logger.debug("Failed to load session variables for %s: %s", session_id, e)
        return {}

    return variables if isinstance(variables, dict) else {}


def _has_explicit_context_loss(
    session_source: str | None,
    variables: dict[str, Any],
) -> bool:
    source = (session_source or "startup").lower()
    if source in _CONTEXT_LOSS_SOURCES:
        return True
    return source == "resume" and variables.get("pending_context_reset") is True


def _has_prior_context_evidence(session: Any | None, variables: dict[str, Any]) -> bool:
    if variables.get("_agent_context_injected") is True:
        return True
    if session is None:
        return False
    if getattr(session, "startup_claim_state", None) == "committed":
        return True
    return _positive_count(getattr(session, "message_count", 0)) or _positive_count(
        getattr(session, "turn_count", 0)
    )


def _positive_count(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.isdigit():
        return int(value) > 0
    return False
