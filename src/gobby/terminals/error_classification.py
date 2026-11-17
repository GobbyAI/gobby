"""Backend-neutral classification of terminal operation failures."""

from __future__ import annotations

from collections.abc import Iterator

_MAX_CHAIN_DEPTH = 16

_SOCKET_PATH_MARKERS = (".sock",)
_VANISHED_TARGET_TYPE_NAMES = frozenset(
    {
        "HostUnavailableError",
    }
)


def iter_exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield an error and its cause/context ancestors, bounded and cycle-safe."""
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(_MAX_CHAIN_DEPTH):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_vanished_terminal_target(error: BaseException) -> bool:
    """Return whether a terminal operation failed because its target went away.

    The native runtime can bury the reason by re-raising as
    TerminalWriteError(stage="none"), whose own message says nothing, so the
    whole chain has to be walked. Matching type names instead of importing the
    backend exceptions keeps this module free of any backend dependency.
    """
    for link in iter_exception_chain(error):
        if isinstance(link, TimeoutError):
            return True
        if type(link).__name__ in _VANISHED_TARGET_TYPE_NAMES:
            return True
        message = str(link).casefold()
        if "no such file or directory" in message:
            # A missing executable is a real misconfiguration, not a race;
            # only a missing socket path means the target itself vanished.
            # Keep walking either way so a later link can still match.
            if any(marker in message for marker in _SOCKET_PATH_MARKERS):
                return True
            continue
    return False
