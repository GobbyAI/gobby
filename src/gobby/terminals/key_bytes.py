"""Byte encodings for NamedKey values shared by the tmux and native runtimes."""

from __future__ import annotations

from gobby.terminals.runtime import NamedKey, TerminalWriteError, is_named_key

__all__ = ["TMUX_KEY_NAMES", "encode_named_key", "normalize_named_key", "tmux_key_name"]

_CURSOR_LETTERS: dict[str, str] = {"up": "A", "down": "B", "right": "C", "left": "D"}
_KEYPAD_APP: dict[str, bytes] = {
    "kp0": b"\x1bOp",
    "kp1": b"\x1bOq",
    "kp2": b"\x1bOr",
    "kp3": b"\x1bOs",
    "kp4": b"\x1bOt",
    "kp5": b"\x1bOu",
    "kp6": b"\x1bOv",
    "kp7": b"\x1bOw",
    "kp8": b"\x1bOx",
    "kp9": b"\x1bOy",
    "kpdecimal": b"\x1bOn",
    "kpminus": b"\x1bOm",
    "kpplus": b"\x1bOk",
    "kpmul": b"\x1bOj",
    "kpdiv": b"\x1bOo",
    "kpenter": b"\x1bOM",
}
_KEYPAD_NORMAL: dict[str, bytes] = {
    "kp0": b"0",
    "kp1": b"1",
    "kp2": b"2",
    "kp3": b"3",
    "kp4": b"4",
    "kp5": b"5",
    "kp6": b"6",
    "kp7": b"7",
    "kp8": b"8",
    "kp9": b"9",
    "kpdecimal": b".",
    "kpminus": b"-",
    "kpplus": b"+",
    "kpmul": b"*",
    "kpdiv": b"/",
    "kpenter": b"\r",
}
# Keys with one fixed byte sequence regardless of terminal mode.
_FIXED_BYTES: dict[str, bytes] = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "ctrl_c": b"\x03",
    "ctrl_k": b"\x0b",
    "ctrl_l": b"\x0c",
    "ctrl_u": b"\x15",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
}


def encode_named_key(key: NamedKey, *, cursor_app: bool = False, keypad_app: bool = False) -> bytes:
    """Encode a NamedKey as the bytes a terminal application expects.

    ``cursor_app`` and ``keypad_app`` select the DECCKM / DECKPAM sequences for
    arrows and keypad keys; every other key has one encoding.
    """
    fixed = _FIXED_BYTES.get(key)
    if fixed is not None:
        return fixed
    letter = _CURSOR_LETTERS.get(key)
    if letter is not None:
        prefix = b"\x1bO" if cursor_app else b"\x1b["
        return prefix + letter.encode("ascii")
    table = _KEYPAD_APP if keypad_app else _KEYPAD_NORMAL
    encoded = table.get(key)
    if encoded is None:
        raise TerminalWriteError(stage="none")
    return encoded


# tmux send-keys names for the NamedKeys Gobby injects into composers.
TMUX_KEY_NAMES: dict[NamedKey, str] = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "BSpace",
    "delete": "DC",
    "ctrl_c": "C-c",
    "ctrl_k": "C-k",
    "ctrl_l": "C-l",
    "ctrl_u": "C-u",
}

_NAMED_KEYS_BY_TMUX_NAME = {name.lower(): key for key, name in TMUX_KEY_NAMES.items()}


def normalize_named_key(value: str) -> NamedKey | None:
    """Accept the internal vocabulary and equivalent tmux-style key names."""
    normalized = value.lower()
    if is_named_key(normalized):
        return normalized
    return _NAMED_KEYS_BY_TMUX_NAME.get(normalized)


def tmux_key_name(key: NamedKey) -> str | None:
    """Return the tmux ``send-keys`` name for ``key``, or None when tmux has none."""
    return TMUX_KEY_NAMES.get(key)
