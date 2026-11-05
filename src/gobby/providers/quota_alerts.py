"""Provider-neutral quota edges delivered to the operator channel.

Edges are computed under the provider capacity lock from the persisted alert state and
delivered after the row write commits. Delivery is best effort: a failed, slow, or absent
channel never changes quota state.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from gobby.config.communications import CommunicationsConfig
from gobby.providers.quota_observations import QuotaComponent, QuotaObservation
from gobby.providers.quota_signals import PaneQuotaHint

logger = logging.getLogger(__name__)

WARN_PERCENT = 90.0
HINT_COOLDOWN = timedelta(seconds=900)
_RANK: dict[str, int] = {"ok": 0, "warn": 1, "exhausted": 2}
_NOUNS: dict[str, str] = {"warn": "warning", "exhausted": "exhausted", "reset": "reset"}

type AlertKind = Literal[
    "warn",
    "exhausted",
    "limit_reached",
    "drawing_credits",
    "reset",
    "possible_limit",
]


@dataclass(frozen=True, slots=True)
class AlertEdge:
    """One rising edge. Measured fields stay None unless the provider supplied them."""

    provider: str
    kind: AlertKind
    window: str | None = None
    percent: int | None = None
    resets_at: str | None = None
    credit_balance: str | None = None


@dataclass(frozen=True, slots=True)
class AlertTransition:
    """Persisted alert state after one transition, plus the edges it raises."""

    details: dict[str, object] = field(default_factory=dict)
    edges: tuple[AlertEdge, ...] = ()


type AlertSink = Callable[[AlertEdge], Awaitable[None]]


class OperatorChannel(Protocol):
    """The one channel operation the sink needs; CommunicationsManager satisfies it."""

    async def send_message(self, channel_name: str, content: str) -> object: ...


def transition(
    previous_details: Mapping[str, object],
    observation: QuotaObservation,
    *,
    now: datetime,
) -> AlertTransition:
    """Return the next persisted alert state and the edges this observation raises.

    The observation carries only admitted components. Window levels rise monotonically
    within one reset epoch and rearm when a later reset arrives. A hard episode alerts
    once per scope and limit until newer positive evidence for that same key clears it.
    """
    provider = observation.provider
    state = _load(previous_details)
    edges: list[AlertEdge] = []
    for component in observation.components:
        if component.window is not None:
            _window_transition(state, provider, component, edges)
        elif component.state == "exhausted":
            _episode_transition(state, provider, component, edges)
        elif component.verified_recovery and component.state == "available":
            _recovery_transition(state, provider, component, edges)
        else:
            _warning_transition(state, provider, component, edges)
    _credit_transition(state, provider, observation, now, edges)
    return AlertTransition(details=state, edges=tuple(edges))


def transition_hint(
    previous_details: Mapping[str, object],
    hint: PaneQuotaHint,
    *,
    now: datetime,
) -> AlertTransition:
    """Return the next hint cooldown state and the possible-limit edge, if any.

    Hints never touch quota components, so they never rearm or clear a verified incident.
    """
    state = _load(previous_details)
    last = _parse_time(state.get("hint_notified_at"))
    if last is not None and now - last < HINT_COOLDOWN:
        return AlertTransition(details=state)
    state["hint_notified_at"] = now.isoformat()
    edge = AlertEdge(provider=hint.provider, kind="possible_limit")
    return AlertTransition(details=state, edges=(edge,))


def render_alert(edge: AlertEdge) -> str:
    """Render the minimal operator text for one edge."""
    head = f"{edge.provider}: "
    if edge.kind == "possible_limit":
        return f"{head}possible quota limit"
    if edge.kind == "limit_reached":
        return f"{head}limit reached"
    if edge.kind == "drawing_credits":
        if edge.credit_balance is None:
            return f"{head}credits drawn"
        return f"{head}credits drawn, balance {edge.credit_balance}"
    subject = f"{edge.window} " if edge.window else "quota "
    text = f"{head}{subject}{_NOUNS.get(edge.kind, edge.kind)}"
    if edge.percent is not None:
        text += f" at {edge.percent}%"
    if edge.resets_at is not None:
        text += f", resets {edge.resets_at}"
    return text


def build_operator_alert_sink(
    *,
    manager: Callable[[], OperatorChannel | None],
    config: Callable[[], CommunicationsConfig],
    timeout_seconds: float = 10.0,
) -> AlertSink:
    """Build the single bounded sink; getters are read at send time, not at construction.

    Disabled communications, a missing manager, or an empty channel fall back to INFO.
    Delivery errors and timeouts are logged and never propagate.
    """

    async def send(edge: AlertEdge) -> None:
        text = render_alert(edge)
        channel = config().operator_alert_channel
        communications = manager()
        if communications is None or not channel:
            logger.info("operator alert not delivered, channel unavailable: %s", text)
            return
        try:
            await asyncio.wait_for(communications.send_message(channel, text), timeout_seconds)
        except TimeoutError:
            logger.warning("operator alert delivery timed out after %ss", timeout_seconds)
        except Exception as exc:
            logger.warning("operator alert delivery failed: %s", type(exc).__name__)

    return send


def _window_transition(
    state: dict[str, object],
    provider: str,
    component: QuotaComponent,
    edges: list[AlertEdge],
) -> None:
    window = component.window
    if window is None or window.limit <= 0:
        return
    percent = window.used / window.limit * 100.0
    if window.used >= window.limit:
        level = "exhausted"
    elif percent >= WARN_PERCENT:
        level = "warn"
    else:
        level = "ok"
    windows = _section(state, "windows")
    prior = _mapping(windows.get(component.key))
    prior_level = str(prior.get("level", "ok"))
    prior_epoch = _parse_time(prior.get("epoch"))
    epoch = component.resets_at
    if epoch is not None and prior_epoch is not None and epoch > prior_epoch:
        if prior_level != "ok":
            edges.append(AlertEdge(provider=provider, kind="reset", window=window.label))
        prior_level = "ok"
    new_level = prior_level
    if _RANK[level] > _RANK.get(prior_level, 0):
        new_level = level
        edges.append(
            AlertEdge(
                provider=provider,
                kind="exhausted" if level == "exhausted" else "warn",
                window=window.label,
                percent=int(percent),
            )
        )
    kept_epoch = epoch if epoch is not None else prior_epoch
    windows[component.key] = {
        "scope": component.scope,
        "level": new_level,
        "epoch": kept_epoch.isoformat() if kept_epoch is not None else None,
    }


def _episode_transition(
    state: dict[str, object],
    provider: str,
    component: QuotaComponent,
    edges: list[AlertEdge],
) -> None:
    episodes = _section(state, "episodes")
    reached = _reached(component.metadata)
    prior = episodes.get(component.key)
    if prior is None:
        episodes[component.key] = {"reached": reached}
        edges.append(AlertEdge(provider=provider, kind="exhausted"))
        if reached:
            edges.append(AlertEdge(provider=provider, kind="limit_reached"))
        return
    if reached and _mapping(prior).get("reached") is not True:
        episodes[component.key] = {"reached": True}
        edges.append(AlertEdge(provider=provider, kind="limit_reached"))


def _recovery_transition(
    state: dict[str, object],
    provider: str,
    component: QuotaComponent,
    edges: list[AlertEdge],
) -> None:
    episodes = _section(state, "episodes")
    if episodes.pop(component.key, None) is not None:
        edges.append(AlertEdge(provider=provider, kind="reset"))


def _warning_transition(
    state: dict[str, object],
    provider: str,
    component: QuotaComponent,
    edges: list[AlertEdge],
) -> None:
    warned = _section(state, "warned")
    if component.metadata.get("warning") is True:
        if component.key not in warned:
            warned[component.key] = True
            edges.append(AlertEdge(provider=provider, kind="warn"))
        return
    warned.pop(component.key, None)


def _credit_transition(
    state: dict[str, object],
    provider: str,
    observation: QuotaObservation,
    now: datetime,
    edges: list[AlertEdge],
) -> None:
    credits: dict[str, Mapping[str, object]] = {}
    for component in observation.components:
        raw = component.metadata.get("credits")
        if isinstance(raw, Mapping):
            credits.setdefault(component.scope, raw)
    ledger = _section(state, "credits")
    for scope, raw in credits.items():
        prior = _mapping(ledger.get(scope))
        drawing = prior.get("drawing") is True
        prior_value = _number(prior.get("value"))
        raw_balance = raw.get("balance")
        text = raw_balance if isinstance(raw_balance, str) else None
        value = _balance_value(text)
        has_credits = raw.get("has_credits")
        if has_credits is False:
            drawing = False
        elif has_credits is True and value is not None and prior_value is not None:
            if value < prior_value:
                if not drawing and _scope_exhausted(state, scope, now):
                    edges.append(
                        AlertEdge(provider=provider, kind="drawing_credits", credit_balance=text)
                    )
                    drawing = True
            else:
                drawing = False
        ledger[scope] = {
            "balance": text if value is not None else prior.get("balance"),
            "value": value if value is not None else prior_value,
            "drawing": drawing,
        }


def _scope_exhausted(state: Mapping[str, object], scope: str, now: datetime) -> bool:
    for entry in _mapping(state.get("windows")).values():
        item = _mapping(entry)
        if item.get("scope") != scope or item.get("level") != "exhausted":
            continue
        epoch = _parse_time(item.get("epoch"))
        if epoch is None or epoch > now:
            return True
    return False


def _reached(metadata: Mapping[str, object]) -> bool:
    reached_type = metadata.get("rate_limit_reached_type")
    return (isinstance(reached_type, str) and bool(reached_type)) or (
        metadata.get("spend_control_reached") is True
    )


def _balance_value(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _section(state: dict[str, object], name: str) -> dict[str, object]:
    section = state.get(name)
    if not isinstance(section, dict):
        section = {}
        state[name] = section
    return section


def _load(previous_details: Mapping[str, object]) -> dict[str, object]:
    raw = previous_details.get("quota_alerts")
    return copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
