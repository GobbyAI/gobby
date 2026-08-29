"""Backend-neutral classification of terminal operation failures."""

from __future__ import annotations

from collections.abc import Iterator

_MAX_CHAIN_DEPTH = 16

_VANISHED_TARGET_MARKERS = (
    "can't find pane",
    "can't find session",
    "failed to connect to server",
    "no server running on",
)
_SOCKET_PATH_MARKERS = ("tmux-", "tmux socket", ".sock")
_VANISHED_TARGET_TYPE_NAMES = frozenset(
    {
        "HostUnavailableError",
        "TmuxTargetUnavailableError",
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

    Both backends bury the reason: the tmux and native runtimes re-raise as
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
            # A missing tmux binary is a real misconfiguration, not a race;
            # only a missing socket path means the target itself vanished.
            # Keep walking either way so a later link can still match.
            if any(marker in message for marker in _SOCKET_PATH_MARKERS):
                return True
            continue
        if any(marker in message for marker in _VANISHED_TARGET_MARKERS):
            return True
    return False
