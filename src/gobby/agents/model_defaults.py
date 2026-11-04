"""The project's model policy: one map keyed by agent name, and a denylist of model names.

Both live in the ``project_policy`` value. ``agent_model_defaults`` maps an agent name to the
provider, model and reasoning effort its launches take by default, with ordered fallbacks.
``default_model_denylist`` names model-name tokens that a default may not use; an explicit
spawn model is never checked against it. This module only parses and matches. The spawn
resolver and the runbook seat check read it, and nothing else does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelChoice:
    """One provider, model and reasoning effort a launch can run.

    ``reasoning_effort`` None means the choice names no effort, so the launch uses the effort
    of the choice it replaces: the primary's effort for a fallback.
    """

    provider: str
    model: str
    reasoning_effort: str | None


@dataclass(frozen=True)
class PolicyEntry:
    """The policy default for one agent: its primary choice and its ordered fallbacks."""

    provider: str
    model: str
    reasoning_effort: str | None
    fallbacks: tuple[ModelChoice, ...] = ()


@dataclass(frozen=True)
class ModelDefaults:
    """The policy's model defaults: the agent map and the denylist."""

    entries: Mapping[str, PolicyEntry] = field(default_factory=dict)
    denylist: tuple[str, ...] = ()

    def entry_for(self, agent: str | None) -> PolicyEntry | None:
        """The map entry for ``agent``, or None when the map names none."""
        return self.entries.get(agent) if agent else None


_ENTRY_KEYS = frozenset({"provider", "model", "reasoning_effort", "fallbacks"})
_CHOICE_KEYS = frozenset({"provider", "model", "reasoning_effort"})
_NAME_SEPARATORS = re.compile(r"[-._/:]+")


def is_denylisted(model: str, denylist: Iterable[str]) -> bool:
    """Whether ``model`` carries a denylisted token, such as ``fable`` in ``claude-fable-5-1``.

    Tokens split on ``-``, ``.``, ``_``, ``/`` and ``:``. A bracket suffix such as ``[1m]`` is
    not part of the name. A term matches whole tokens only, never a substring of one.
    """
    name = model.strip().lower().split("[", 1)[0]
    tokens = {token for token in _NAME_SEPARATORS.split(name) if token}
    return any(term.strip().lower() in tokens for term in denylist if term.strip())


def model_defaults_from_policy(policy: Mapping[str, Any]) -> ModelDefaults:
    """Parse the map and denylist from a ``project_policy`` value. Raises ``ValueError``.

    A malformed entry is refused, never skipped: a skipped entry would quietly send that agent
    to its definition's model. Other policy keys are not read.
    """
    raw_entries = policy.get("agent_model_defaults", {})
    if not isinstance(raw_entries, Mapping):
        raise ValueError("agent_model_defaults must map agent names to entries")
    raw_denylist = policy.get("default_model_denylist", [])
    if not isinstance(raw_denylist, list) or not all(
        isinstance(term, str) for term in raw_denylist
    ):
        raise ValueError("default_model_denylist must be a list of model name tokens")
    entries = {agent: _entry(agent, raw) for agent, raw in raw_entries.items()}
    denylist = tuple(term.strip().lower() for term in raw_denylist if term.strip())
    return ModelDefaults(entries=entries, denylist=denylist)


def _entry(agent: str, raw: object) -> PolicyEntry:
    where = f"agent_model_defaults.{agent}"
    fields = _checked(where, raw, _ENTRY_KEYS)
    raw_fallbacks = fields.pop("fallbacks", [])
    if not isinstance(raw_fallbacks, list):
        raise ValueError(f"{where}.fallbacks must be a list")
    primary = _choice(where, fields)
    fallbacks = tuple(
        _choice(f"{where}.fallbacks[{index}]", raw_fallback)
        for index, raw_fallback in enumerate(raw_fallbacks)
    )
    return PolicyEntry(
        provider=primary.provider,
        model=primary.model,
        reasoning_effort=primary.reasoning_effort,
        fallbacks=fallbacks,
    )


def _choice(where: str, raw: object) -> ModelChoice:
    fields = _checked(where, raw, _CHOICE_KEYS)
    provider = fields.get("provider")
    model = fields.get("model")
    effort = fields.get("reasoning_effort")
    if not _is_name(provider) or not _is_name(model):
        raise ValueError(f"{where} needs a provider and a model")
    if effort is not None and not _is_name(effort):
        raise ValueError(f"{where}.reasoning_effort must be a non-empty string")
    return ModelChoice(provider=str(provider), model=str(model), reasoning_effort=effort)


def _checked(where: str, raw: object, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(unknown)}")
    return dict(raw)


def _is_name(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""
