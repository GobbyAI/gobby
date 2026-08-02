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

from gobby._generated_tool_loop_limits import (
    DEFAULT_LOOP_TIMEOUT_SECONDS,
    DEFAULT_MAX_BYTES_PER_TOOL_RESULT,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ToolLoopLimitsDict,
)
from gobby.config.feature_base import FeatureCandidateInput

MAX_TURNS_STOP_REASON = "max_turns"
MAX_TOOL_CALLS_STOP_REASON = "max_tool_calls"
TIMEOUT_STOP_REASON = "timeout"
LIMIT_STOP_REASONS = frozenset(
    {MAX_TURNS_STOP_REASON, MAX_TOOL_CALLS_STOP_REASON, TIMEOUT_STOP_REASON}
)

if TYPE_CHECKING:
    from gobby.ai._tool_chat_builtins import BuiltinToolSpec, InvocationRecord
    from gobby.ai.registry import AIAdapterStyle, CapabilityBinding


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

    max_turns: int | None = DEFAULT_MAX_TURNS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_bytes_per_tool_result: int = DEFAULT_MAX_BYTES_PER_TOOL_RESULT
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    loop_timeout_seconds: int = DEFAULT_LOOP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        from gobby.ai._tool_chat_builtins import minimum_typed_error_result_size

        values = {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_bytes_per_tool_result": self.max_bytes_per_tool_result,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "loop_timeout_seconds": self.loop_timeout_seconds,
        }
        for name, value in values.items():
            if value is not None and value <= 0:
                raise ToolLoopConfigurationError(f"{name} must be positive")
        minimum_cap = minimum_typed_error_result_size()
        if self.max_bytes_per_tool_result < minimum_cap:
            raise ToolLoopConfigurationError(
                "max_bytes_per_tool_result must be at least "
                f"{minimum_cap} bytes to carry a typed error result"
            )

    def as_dict(self) -> ToolLoopLimitsDict:
        return {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_bytes_per_tool_result": self.max_bytes_per_tool_result,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "loop_timeout_seconds": self.loop_timeout_seconds,
        }


class ToolLoopConfigurationError(ValueError):
    """Raised when tool-loop limits cannot satisfy the result contract."""


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
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    limits: ToolLoopLimits | None = None
    builtins: tuple[BuiltinToolSpec, ...] = ()
    allowed_adapter_styles: tuple[AIAdapterStyle, ...] | None = None
    caller: str | None = None
    request_id: str | None = None

    @property
    def effective_limits(self) -> ToolLoopLimits:
        return self.limits or ToolLoopLimits()


@dataclass(frozen=True, kw_only=True)
class ToolChatResult:
    """Result of a ``tool_chat`` run plus investigation provenance.

    ``text`` is the grounded narrative when the provider produced one; limit-only
    results may omit it. The remaining fields describe how the investigation ran
    so callers can surface provenance and the route used.
    ``adapter_style`` records which adapter family executed (for observability
    only — callers must not branch on it).
    """

    text: str | None
    provider: str | None = None
    model: str | None = None
    adapter_style: str | None = None
    tool_use_count: int = 0
    turns: int | None = None
    tools: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    applied_reasoning_effort: str | None = None
    stop_reason: str | None = None
    trace: tuple[InvocationRecord, ...] = ()
    calls_used: int = 0
    budget_exhausted: bool = False
    trace_available: bool = False


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
