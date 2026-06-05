"""Per-CLI plan-accept option registry.

When a managed CLI presents a plan in web-chat plan mode, each CLI actually
offers a *richer, different* set of choices at plan acceptance than a single
generic Approve / Request changes. This module is the documented source of
truth for those option sets, keyed by :class:`SessionSource`.

The design mirrors the :data:`PROVIDER_CAPABILITIES` registry in
``capabilities.py`` but is kept in its own module so each can evolve without
bloating the other (capabilities.py is near the monolith limit).

Architecture:

* **Single source of truth = this backend registry.** The backend emits the
  available option list into the ``plan_pending_approval`` broadcast; the
  frontend renders whatever it is given. No parallel TS registry to drift.
* **Option -> action primitives.** Every option maps onto the uniform Gobby
  actions: ``decision`` (``approve`` | ``keep_planning``),
  ``post_plan_chat_mode`` (``plan`` | ``normal`` | ``accept_edits`` |
  ``bypass``), ``auto_continue`` and ``clear_context``. The per-CLI *option
  sets* differ; the *primitives* are shared.

A CLI changing its options is therefore a data edit here, not a UI rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.adapters.capabilities import normalize_source
from gobby.hooks.events import SessionSource


@dataclass(frozen=True)
class PlanAcceptOption:
    """One selectable choice presented when a plan is awaiting approval.

    Attributes:
        id: Stable identifier echoed back in ``plan_approval_response`` as
            ``option_id``. Never reuse an id for a different meaning.
        label: Button text shown to the user.
        description: Tooltip / sub-label describing the consequence.
        decision: ``"approve"`` exits plan mode; ``"keep_planning"`` keeps the
            plan unapproved and re-enters planning (reuses the request-changes
            path without requiring typed feedback).
        post_plan_chat_mode: The chat mode the session lands in after the
            option is applied (``"plan"`` | ``"normal"`` | ``"accept_edits"``
            | ``"bypass"``).
        auto_continue: Whether approval should inject a continuation turn so
            the agent proceeds immediately (managed CLIs only; native Claude
            continues its paused turn itself).
        clear_context: Whether to reset the conversation context before
            continuing (Codex "approve + clear context"; a real thread reset,
            not a stub).
        escalate: ``keep_planning`` options that re-enter planning at greater
            depth (Claude "refine with Ultraplan"). The handler seeds a
            deeper-analysis directive for the next planning turn.
    """

    id: str
    label: str
    description: str
    decision: str
    post_plan_chat_mode: str
    auto_continue: bool
    clear_context: bool = False
    escalate: bool = False

    def serialize(self) -> dict[str, object]:
        """Frontend-facing projection (id/label/description/decision only).

        The action primitives (``post_plan_chat_mode`` etc.) stay server-side
        and are re-resolved by :func:`get_plan_accept_option` when the option
        id comes back, so the wire payload never trusts the client for them.
        ``decision`` is included so the UI can style approve vs keep-planning
        buttons (positive primary vs neutral) without hardcoding ids.
        """
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "decision": self.decision,
        }


# Canonical "approve & continue" fallback used for sources without a bespoke
# option set (pipeline/agy) so every surface degrades gracefully to a single
# Approve button.
_GENERIC_APPROVE = PlanAcceptOption(
    id="approve",
    label="Approve & Execute",
    description="Approve the plan and continue.",
    decision="approve",
    post_plan_chat_mode="normal",
    auto_continue=True,
)


def _claude_options() -> list[PlanAcceptOption]:
    # Confirmed from the live SDK ExitPlanMode menu. The canonical
    # approve-and-continue default is listed first.
    return [
        PlanAcceptOption(
            id="approve_manual",
            label="Approve, manually approve edits",
            description="Exit plan mode; prompt before non-exempt tool use.",
            decision="approve",
            post_plan_chat_mode="normal",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="approve_accept_edits",
            label="Approve, auto-accept edits",
            description="Exit plan mode and auto-accept file edits.",
            decision="approve",
            post_plan_chat_mode="accept_edits",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="approve_bypass",
            label="Approve, bypass permissions",
            description="Exit plan mode and run without approval prompts.",
            decision="approve",
            post_plan_chat_mode="bypass",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="ultraplan",
            label="Refine with Ultraplan",
            description="Keep planning and re-plan at greater depth.",
            decision="keep_planning",
            post_plan_chat_mode="plan",
            auto_continue=False,
            escalate=True,
        ),
    ]


def _codex_options() -> list[PlanAcceptOption]:
    # Codex plan-collaboration mode: approve / approve + clear-context /
    # keep-planning. Clear-context is a real thread reset reseeded with the
    # approved plan (implemented in the Codex web-chat backend).
    return [
        PlanAcceptOption(
            id="approve",
            label="Approve & implement",
            description="Exit plan mode and implement in the same context.",
            decision="approve",
            post_plan_chat_mode="normal",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="approve_clear_context",
            label="Approve, clear context & implement",
            description="Reset the conversation to the approved plan, then implement.",
            decision="approve",
            post_plan_chat_mode="normal",
            auto_continue=True,
            clear_context=True,
        ),
        PlanAcceptOption(
            id="keep_planning",
            label="Keep planning",
            description="Stay in plan mode and continue refining.",
            decision="keep_planning",
            post_plan_chat_mode="plan",
            auto_continue=False,
        ),
    ]


def _acp_approval_mode_options() -> list[PlanAcceptOption]:
    # ACP CLIs (Gemini, Qwen, Grok, Droid) accept a plan by choosing the new
    # approval mode (default / auto-edit / yolo). Reject-with-feedback stays on
    # the separate request-changes path. The default mode is listed first.
    return [
        PlanAcceptOption(
            id="approve_default",
            label="Approve",
            description="Exit plan mode; prompt before non-exempt tool use.",
            decision="approve",
            post_plan_chat_mode="normal",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="approve_auto_edit",
            label="Approve, auto-accept edits",
            description="Exit plan mode and auto-accept file edits.",
            decision="approve",
            post_plan_chat_mode="accept_edits",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="approve_yolo",
            label="Approve, YOLO",
            description="Exit plan mode and run without approval prompts.",
            decision="approve",
            post_plan_chat_mode="bypass",
            auto_continue=True,
        ),
        PlanAcceptOption(
            id="keep_planning",
            label="Keep planning",
            description="Stay in plan mode and continue refining.",
            decision="keep_planning",
            post_plan_chat_mode="plan",
            auto_continue=False,
        ),
    ]


PLAN_ACCEPT_OPTIONS: dict[SessionSource, list[PlanAcceptOption]] = {
    SessionSource.CLAUDE: _claude_options(),
    SessionSource.CODEX: _codex_options(),
    SessionSource.GEMINI: _acp_approval_mode_options(),
    SessionSource.QWEN: _acp_approval_mode_options(),
    SessionSource.GROK: _acp_approval_mode_options(),
    SessionSource.DROID: _acp_approval_mode_options(),
    SessionSource.AGY: [_GENERIC_APPROVE],
    SessionSource.PIPELINE: [_GENERIC_APPROVE],
}


def _normalize_plan_source(source: SessionSource | str) -> SessionSource:
    """Normalize a source for option lookup.

    Accepts a :class:`SessionSource`, a bare provider string (``"droid"``), or
    the ``"<provider>_web_chat"`` form returned by managed-session
    ``_web_chat_source()`` helpers. Unknown sources fall back to
    :data:`SessionSource.PIPELINE` so the caller still receives the generic
    approve option rather than raising.
    """
    if isinstance(source, SessionSource):
        return source
    candidate = source
    if candidate.endswith("_web_chat"):
        candidate = candidate[: -len("_web_chat")]
    try:
        return normalize_source(candidate)
    except ValueError:
        return SessionSource.PIPELINE


def get_plan_accept_options(source: SessionSource | str) -> list[PlanAcceptOption]:
    """Return the ordered option set for a source.

    The first entry is always the canonical "approve & continue" default so the
    UI degrades gracefully. Unknown sources yield the single generic approve.
    """
    normalized = _normalize_plan_source(source)
    return PLAN_ACCEPT_OPTIONS.get(normalized, [_GENERIC_APPROVE])


def get_plan_accept_option(
    source: SessionSource | str, option_id: str | None
) -> PlanAcceptOption | None:
    """Resolve a single option by id for a source.

    Returns ``None`` when ``option_id`` is falsy or does not belong to the
    source's set, so callers can fall back to the legacy generic-approve
    behavior (backward compatible with clients that omit ``option_id``).
    """
    if not option_id:
        return None
    for option in get_plan_accept_options(source):
        if option.id == option_id:
            return option
    return None


def serialize_plan_accept_options(source: SessionSource | str) -> list[dict[str, object]]:
    """Serialize a source's option set for the ``plan_pending_approval`` payload."""
    return [option.serialize() for option in get_plan_accept_options(source)]


__all__ = [
    "PLAN_ACCEPT_OPTIONS",
    "PlanAcceptOption",
    "get_plan_accept_option",
    "get_plan_accept_options",
    "serialize_plan_accept_options",
]
