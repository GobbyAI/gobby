"""Strip the outer alternate-screen switch from a tmux client's output.

``tmux attach-session`` opens its stream with ``ESC [ ? 1049 h`` -- the terminfo
``smcup`` capability -- which moves the receiving terminal to the alternate
screen for the whole attached lifetime. The alternate screen has no scrollback
by definition, so restored history written before the switch is retained by the
VT and never reachable: the web terminal reports zero scrollback lines while
attached, and every one of them reappears the moment the switch is undone.

tmux has no client-scoped way to suppress this. ``attach-session -T`` sets
*terminal-features*, which has no alternate-screen entry, and
``terminal-overrides`` is a server option -- setting ``smcup@:rmcup@`` there
would change behavior for every client of the user's tmux server, including
their own terminals. Removing the switch from this bridge's own byte stream
gets the same result scoped to the one client that needs it.
"""

from __future__ import annotations

# The switch pairs a terminal may be moved between screens with. 1049 is what
# modern terminfo emits; 47 and 1047 are the historical spellings, stripped too
# so an unusual terminfo entry cannot reintroduce the same defect.
ALT_SCREEN_SWITCHES = (
    "\x1b[?1049h",
    "\x1b[?1049l",
    "\x1b[?1047h",
    "\x1b[?1047l",
    "\x1b[?47h",
    "\x1b[?47l",
)

_LONGEST_SWITCH = max(len(switch) for switch in ALT_SCREEN_SWITCHES)


class AltScreenFilter:
    """Remove alternate-screen switches from a chunked terminal stream.

    Instances are stateful and belong to exactly one stream: a switch can be
    split across two ``os.read`` boundaries, so a trailing fragment that could
    still become one is carried into the next chunk instead of forwarded. A
    fragment that turns out to be something else is emitted intact, in order,
    on the call that resolves it.
    """

    def __init__(self) -> None:
        self._carry = ""

    def __call__(self, text: str) -> str:
        """Return ``text`` with any complete switch removed.

        A trailing partial switch is withheld until the next call. At end of
        stream the carry is dropped, which is correct: an escape sequence that
        never completed has nothing to render.
        """
        buffer = self._carry + text
        self._carry = ""
        if "\x1b" not in buffer:
            return buffer

        pieces: list[str] = []
        index = 0
        while True:
            escape = buffer.find("\x1b", index)
            if escape == -1:
                pieces.append(buffer[index:])
                break

            pieces.append(buffer[index:escape])
            matched = next(
                (s for s in ALT_SCREEN_SWITCHES if buffer.startswith(s, escape)),
                None,
            )
            if matched is not None:
                index = escape + len(matched)
                continue

            tail = buffer[escape:]
            if len(tail) < _LONGEST_SWITCH and any(
                switch.startswith(tail) for switch in ALT_SCREEN_SWITCHES
            ):
                # Still could become a switch once more bytes arrive.
                self._carry = tail
                break

            # Some other escape sequence. Forward the ESC and resume scanning
            # from the next character so a switch nested behind it is still
            # found.
            pieces.append(buffer[escape])
            index = escape + 1

        return "".join(pieces)
