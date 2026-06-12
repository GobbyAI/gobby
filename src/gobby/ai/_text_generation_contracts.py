"""Contracts shared by daemon text generation modules."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult


@dataclass(frozen=True, kw_only=True)
class TextGenerationRequest:
    """One daemon text_generate request."""

    prompt: str
    provider: str | None = None
    profile: str | None = None
    candidates: tuple[str, ...] = ()
    system_prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    caller: str | None = None
    cwd: str | None = None


class TextGenerateAdapter(Protocol):
    """Adapter for one provider's text_generate execution path."""

    async def generate(self, request: TextGenerationRequest) -> str | LLMTextResult:
        """Generate text for the request."""


class TextGenerateJSONAdapter(Protocol):
    """Adapter with provider-native JSON generation support."""

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        """Generate and parse JSON for the request."""


TextGenerateAdapterFactory = Callable[[], TextGenerateAdapter]


class ACPStreamEventLike(Protocol):
    """Subset of normalized ACP stream events used by text generation."""

    @property
    def event_type(self) -> str:
        """Return the normalized event type."""

    @property
    def data(self) -> Mapping[str, Any]:
        """Return the normalized event payload."""


class ACPClientLike(Protocol):
    """Subset of ACP clients used by text generation."""

    async def start(
        self,
        session_id: str | None = None,
        model: str | None = None,
        *,
        auto_session: bool = True,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Start the ACP client."""

    def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        pre_tool_callback: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> AsyncIterator[ACPStreamEventLike]:
        """Send a prompt and stream normalized events."""

    async def stop(self) -> None:
        """Stop the ACP client."""


ACPClientFactory = Callable[[], ACPClientLike]


class CodexAppServerClientLike(Protocol):
    """Subset of Codex app-server client used by text_generate."""

    @property
    def is_connected(self) -> bool:
        """Return whether the app-server process is connected."""

    async def start(self) -> None:
        """Start the app-server process."""

    async def stop(self) -> None:
        """Stop the app-server process."""

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        ephemeral: bool = False,
    ) -> Any:
        """Start a Codex app-server thread."""

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one Codex app-server turn."""

    async def archive_thread(self, thread_id: str) -> None:
        """Archive a Codex app-server thread."""


CodexAppServerClientFactory = Callable[[], CodexAppServerClientLike]


CodexAppServerClientProvider = Callable[[], CodexAppServerClientLike | None]


__all__ = [
    "ACPClientFactory",
    "ACPClientLike",
    "ACPStreamEventLike",
    "CodexAppServerClientFactory",
    "CodexAppServerClientLike",
    "CodexAppServerClientProvider",
    "TextGenerateAdapter",
    "TextGenerateAdapterFactory",
    "TextGenerateJSONAdapter",
    "TextGenerationRequest",
]
