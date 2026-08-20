"""Python-side terminal dimension bounds (plan 2.2).

Maximum rows/cols/cells keep a full grid inside MAX_FRAME_SIZE under a
documented worst-cell estimate. Matching these integers to the Rust wire
constants is owned by plan 3.2, not this module.
"""

from __future__ import annotations

MAX_FRAME_SIZE = 2 * 1024 * 1024
WORST_CELL_BYTES = 64
MAX_CELLS = MAX_FRAME_SIZE // WORST_CELL_BYTES
MAX_ROWS = 1024
MAX_COLS = 1024
MIN_ROWS = 1
MIN_COLS = 1


class InvalidTerminalDimensionsError(ValueError):
    """Zero, negative, non-integer, or overflow terminal dimensions."""


def validate_dimensions(rows: object, cols: object) -> tuple[int, int]:
    """Reject illegal dimensions before any row write, allocation, or backend call."""
    if isinstance(rows, bool) or isinstance(cols, bool):
        raise InvalidTerminalDimensionsError("terminal dimensions must be integers")
    if not isinstance(rows, int) or not isinstance(cols, int):
        raise InvalidTerminalDimensionsError("terminal dimensions must be integers")
    if rows < MIN_ROWS or cols < MIN_COLS:
        raise InvalidTerminalDimensionsError("terminal dimensions must be at least 1x1")
    if rows > MAX_ROWS or cols > MAX_COLS:
        raise InvalidTerminalDimensionsError("terminal dimensions exceed maximum rows or columns")
    if rows * cols > MAX_CELLS:
        raise InvalidTerminalDimensionsError("terminal dimensions exceed maximum cell product")
    return rows, cols
