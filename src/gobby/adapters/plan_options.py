"""Plan-accept option registry.

When a plan is awaiting approval in web-chat plan mode, the user picks the
post-plan execution mode *at approval time*. The option set is uniform across
every CLI source: two approve actions whose ``post_plan_chat_mode`` carries the
chosen mode, plus a separate Reject (request-changes) action handled outside
this registry.

* **YOLO** (``approve_yolo``) exits plan mode and runs without approval prompts
  (``bypass``). It is the dominant default -- the rules engine and the sandbox
  are the guardrails.
* **Act** (``approve_act``) exits plan mode but still prompts before non-exempt
  tool use (``normal``).

This replaces both the old per-CLI permission-mode menus and the ``/settings``
"after Plan mode" preference: the mode is a per-approval choice, not stored
config.

Architecture:

* **Single source of truth = this backend registry.** The backend emits the
  option list into the ``plan_pending_approval`` broadcast; the frontend
  renders whatever it is given, using ``emphasis`` for button hierarchy. No
  parallel TS registry to drift.
* **Option -> action primitives.** Each option maps onto the uniform Gobby
  actions: ``decision`` (``approve``), ``post_plan_chat_mode`` (``normal`` |
  ``bypass``), ``auto_continue``, and ``emphasis`` (UI hint). ``source`` is
  retained in the lookup signatures so a future per-source divergence has a
  seam, but today every source yields the same set.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.hooks.events import SessionSource


@dataclass(frozen=True)
class PlanAcceptOption:
    """One selectable choice presented when a plan is awaiting approval.

    Attributes:
        id: Stable identifier echoed back in ``plan_approval_response`` as
            ``option_id``. Never reuse an id for a different meaning.
        label: Button text shown to the user.
        description: Tooltip / sub-label describing the consequence.
        decision: ``"approve"`` exits plan mode into ``post_plan_chat_mode``.
        post_plan_chat_mode: The chat mode the session lands in after the
            option is applied (``"normal"`` | ``"bypass"``; ``"plan"`` and
            ``"accept_edits"`` are reserved for forward-compat).
        auto_continue: Whether approval should inject a continuation turn so
            the agent proceeds immediately (managed CLIs only; native Claude
            continues its paused turn itself).
        emphasis: UI button-hierarchy hint -- ``"primary"`` for the dominant
            solid CTA (YOLO), ``"accent"`` for a quieter tinted action (Act).
    """

    id: str
    label: str
    description: str
    decision: str
    post_plan_chat_mode: str
    auto_continue: bool
    emphasis: str = "accent"

    def serialize(self) -> dict[str, object]:
        """Frontend-facing projection.

        The action primitives (``post_plan_chat_mode`` etc.) stay server-side
        and are re-resolved by :func:`get_plan_accept_option` when the option
        id comes back, so the wire payload never trusts the client for them.
        ``decision`` and ``emphasis`` are included so the UI can style the
        button hierarchy without hardcoding ids.
        """
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "decision": self.decision,
            "emphasis": self.emphasis,
        }


def _fixed_plan_options() -> list[PlanAcceptOption]:
    """The uniform plan-accept option set shown for every source.

    Order encodes hierarchy: YOLO is the dominant primary CTA; Act is the
    quieter tinted secondary. Reject is a separate request-changes action (with
    an optional comment) and is not part of this accept set.
    """
    return [
        PlanAcceptOption(
            id="approve_yolo",
            label="Approve (YOLO)",
            description="Exit plan mode and run without approval prompts.",
            decision="approve",
            post_plan_chat_mode="bypass",
            auto_continue=True,
            emphasis="primary",
        ),
        PlanAcceptOption(
            id="approve_act",
            label="Approve (Act)",
            description="Exit plan mode; prompt before non-exempt tool use.",
            decision="approve",
            post_plan_chat_mode="normal",
            auto_continue=True,
            emphasis="accent",
        ),
    ]


_FIXED_PLAN_OPTIONS: list[PlanAcceptOption] = _fixed_plan_options()


def get_plan_accept_options(source: SessionSource | str) -> list[PlanAcceptOption]:
    """Return the plan-accept option set for a source.

    The set is uniform across sources: approval mode is chosen at approval time
    (YOLO vs Act), not per-CLI. ``source`` is retained in the signature so
    callers stay stable and a future per-source divergence has a seam.
    """
    return _FIXED_PLAN_OPTIONS.copy()


def get_plan_accept_option(
    source: SessionSource | str, option_id: str | None
) -> PlanAcceptOption | None:
    """Resolve a single option by id.

    Returns ``None`` when ``option_id`` is falsy or does not belong to the
    option set, so callers can fall back to the legacy generic-approve behavior
    (backward compatible with clients that omit ``option_id``).
    """
    if not option_id:
        return None
    for option in get_plan_accept_options(source):
        if option.id == option_id:
            return option
    return None


def serialize_plan_accept_options(source: SessionSource | str) -> list[dict[str, object]]:
    """Serialize the option set for the ``plan_pending_approval`` payload."""
    return [option.serialize() for option in get_plan_accept_options(source)]


__all__ = [
    "PlanAcceptOption",
    "get_plan_accept_option",
    "get_plan_accept_options",
    "serialize_plan_accept_options",
]
