"""TerminalRuntime registry and public contract surface."""

from __future__ import annotations

from gobby.terminals.runtime import (
    TerminalRuntime,
    UnregisteredBackendError,
)

__all__ = [
    "TerminalRuntime",
    "TerminalRuntimeRegistry",
    "UnregisteredBackendError",
]


class TerminalRuntimeRegistry:
    """Resolves Terminal.backend to a TerminalRuntime implementation."""

    def __init__(self) -> None:
        self._runtimes: dict[str, TerminalRuntime] = {}

    def register(self, runtime: TerminalRuntime) -> None:
        self._runtimes[runtime.backend] = runtime

    def resolve(self, backend: str) -> TerminalRuntime:
        try:
            return self._runtimes[backend]
        except KeyError as exc:
            raise UnregisteredBackendError(backend) from exc
