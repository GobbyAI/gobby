"""Contracts shared by the daemon ``tool_chat`` (agentic) feature.

``tool_chat`` is the provider-agnostic, caller-parameterized peer of one-shot
``text_generate``. The daemon resolves a feature profile to a ``TOOL_CHAT``
capability binding and dispatches purely on the binding's
:class:`~gobby.ai.registry.AIAdapterStyle` — never on a provider name. The
caller supplies a :class:`ToolPolicy` (which tools are exposed and whether
mutation is permitted) plus a system directive, so the feature core hardcodes
no tool set, no prompt, and no global read-only law.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from gobby.config.feature_base import FeatureCandidateInput

if TYPE_CHECKING:
    from gobby.ai.registry import CapabilityBinding


@dataclass(frozen=True, kw_only=True)
class ToolPolicy:
    """Caller-declared description of the tools an agent may use.

    The feature is generic over *what* the agent does; the caller declares the
    investigation surface here. ``cli`` selects the executable family
    (``"gcode"`` or ``"gwiki"``), ``tools`` lists the exposed subcommands, and
    ``allow_mutation`` gates whether mutating subcommands are permitted. A
    read-only caller (codewiki) leaves ``allow_mutation`` False; a write-capable
    caller (a future gwiki compile policy) sets it True and lists the mutating
    subcommands it needs. The policy is validated against the registry whitelist
    in :mod:`gobby.ai._tool_chat_tools`.
    """

    cli: str
    tools: tuple[str, ...]
    allow_mutation: bool = False


@dataclass(frozen=True, kw_only=True)
class ToolLoopLimits:
    """Bounds for a daemon-run tool-calling loop (Family A).

    Mirrors the gcore ``ToolLoopLimits`` defaults so the daemon and standalone
    paths bound investigation identically.
    """

    max_turns: int = 8
    max_tool_calls: int = 24
    per_tool_result_byte_cap: int = 16 * 1024
    tool_timeout_seconds: float = 60.0


@dataclass(frozen=True, kw_only=True)
class ToolChatRequest:
    """One daemon ``tool_chat`` request.

    Mirrors :class:`~gobby.ai._text_generation_contracts.TextGenerationRequest`
    for profile/candidate selection, and adds the caller-parameterized pieces:
    a :class:`ToolPolicy`, the ``project_path`` the tools run in, and the loop
    bounds. ``provider``/``model`` are optional explicit overrides resolved
    through the capability registry — the service never branches on their value.
    """

    prompt: str
    tool_policy: ToolPolicy
    project_path: str
    system_prompt: str | None = None
    provider: str | None = None
    profile: str | None = None
    candidates: tuple[FeatureCandidateInput, ...] = ()
    model: str | None = None
    max_turns: int | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    limits: ToolLoopLimits = field(default_factory=ToolLoopLimits)
    caller: str | None = None
    candidate_timeout_seconds: float | None = None
    cli_candidate_timeout_seconds: float | None = None


def _resolve_max_turns(request: ToolChatRequest, *, default: int) -> int:
    if request.limits.max_turns is not None:
        return request.limits.max_turns
    if request.max_turns is not None:
        return request.max_turns
    return default


@dataclass(frozen=True, kw_only=True)
class ToolChatResult:
    """Result of a ``tool_chat`` run plus investigation provenance.

    ``text`` is the grounded narrative; the remaining fields describe how the
    investigation ran so callers can surface provenance and the route used.
    ``adapter_style`` records which adapter family executed (for observability
    only — callers must not branch on it).
    """

    text: str
    provider: str | None = None
    model: str | None = None
    adapter_style: str | None = None
    tool_use_count: int = 0
    turns: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    applied_reasoning_effort: str | None = None
    stop_reason: str | None = None


class ToolChatAdapter(Protocol):
    """Adapter for one :class:`~gobby.ai.registry.AIAdapterStyle` family.

    The service selects a binding by capability + profile and dispatches to the
    adapter registered for ``binding.adapter_style``. The adapter constructs its
    concrete provider from the binding (provider names are allowed *inside* the
    adapter) and runs the agent under the request's tool policy and directive.
    """

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        """Run the agent for ``request`` using ``binding`` and return the result."""
        ...
