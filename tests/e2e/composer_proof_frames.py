"""Provider reads from a native semantic frame and its atomic cursor."""

from __future__ import annotations

from dataclasses import dataclass

from tests.e2e.composer_proof import ProofRefused


@dataclass(frozen=True)
class ProofFrame:
    ansi: str
    cursor: tuple[int, int]


def read_frame(message: dict[str, object]) -> ProofFrame:
    width, height = message.get("width"), message.get("height")
    cells, cursor = message.get("cells"), message.get("cursor")
    if (
        message.get("type") != "frame"
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width < 1
        or height < 1
        or not isinstance(cells, list)
        or len(cells) != width * height
    ):
        raise ProofRefused("invalid native frame")
    if (
        not isinstance(cursor, dict)
        or cursor.get("visible") is not True
        or not isinstance(cursor.get("x"), int)
        or not isinstance(cursor.get("y"), int)
        or not 0 <= cursor["x"] < width
        or not 0 <= cursor["y"] < height
    ):
        raise ProofRefused("unverified native cursor")
    rows: list[str] = []
    for y in range(height):
        row: list[str] = []
        for cell in cells[y * width : (y + 1) * width]:
            if (
                not isinstance(cell, dict)
                or not isinstance(cell.get("symbol"), str)
                or not isinstance(cell.get("modifier"), int)
            ):
                raise ProofRefused("invalid native cell")
            # Native ratatui 0.30 Modifier bits: DIM=2, REVERSED=64.
            codes = ["0"]
            if cell["modifier"] & 2:
                codes.append("2")
            if cell["modifier"] & 64:
                codes.append("7")
            row.append(f"\x1b[{';'.join(codes)}m{cell['symbol']}")
        rows.append("".join(row) + "\x1b[0m")
    return ProofFrame("\n".join(rows), (cursor["x"], cursor["y"]))
