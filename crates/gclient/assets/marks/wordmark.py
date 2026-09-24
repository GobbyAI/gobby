"""GOBBY and gobby wordmark variants on a quarter-block grid (2 x 2 subcells per cell, 4.2 x 10 px each).

python3 wordmark.py <variant> [text|html|ansi-dark|ansi-light|grid] [lower]
variants: block round shadow roundshadow figlet braille raster rastershadow
"""

import math
import sys

from PIL import Image, ImageDraw, ImageFont

VARIANT = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "text"
LOWER = len(sys.argv) > 3 and sys.argv[3] == "lower"
WORD = "gobby" if LOWER else "GOBBY"
ROWS = 8 if LOWER else 6  # lowercase carries the b's ascender and the g's and y's descenders
CW, CH = 8.4, 20.0
FONT = "/System/Library/Fonts/HelveticaNeue.ttc#9"  # Helvetica Neue Condensed Black

# unit letterforms: one unit is two cells wide and one row tall
UPPER = {
    "G": [".####", "#....", "#....", "#..##", "#...#", ".####"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", ".###."],
    "B": ["####.", "#...#", "####.", "#...#", "#...#", "####."],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#.."],
}
LOWERCASE = {  # x-height rows 2 to 5, ascender rows 0 and 1, descender rows 6 and 7
    "g": ["....", "....", ".###", "#..#", "#..#", ".###", "...#", ".###"],
    "o": ["....", "....", ".##.", "#..#", "#..#", ".##.", "....", "...."],
    "b": ["#...", "#...", "###.", "#..#", "#..#", "###.", "....", "...."],
    "y": ["....", "....", "#..#", "#..#", "#..#", ".###", "...#", ".###"],
}
GAP = 1  # units between letters


def units():
    forms = LOWERCASE if LOWER else UPPER
    filled = set()
    u0 = 0
    for ch in WORD:
        rows = forms[ch]
        for r, row in enumerate(rows):
            for u, c in enumerate(row):
                if c == "#":
                    filled.add((u0 + u, r))
        u0 += len(rows[0]) + GAP
    return filled


def block_bits(chamfer=False):
    """Units to subcells. chamfer cuts one quarter block off every convex corner; a corner that
    joins a diagonal neighbour (the Y's arms, the g's tail) is kept."""
    filled = units()
    bits = set()
    for u, r in filled:
        bits.update((u * 4 + dx, r * 2 + dy) for dx in range(4) for dy in range(2))
    if chamfer:
        for u, r in filled:
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                if (u + dx, r) in filled or (u, r + dy) in filled or (u + dx, r + dy) in filled:
                    continue
                bits.discard((u * 4 + (0 if dx < 0 else 3), r * 2 + (0 if dy < 0 else 1)))
    return bits


def raster_bits(spec, sub_w, sub_h, sub_rows):
    """Rasterise the word with a font and keep every subcell the glyphs cover by half or more."""
    path, _, index = spec.partition("#")
    font = ImageFont.truetype(path, 400, index=int(index or 0))
    left, top, right, bottom = font.getbbox(WORD)
    img = Image.new("L", (right - left + 40, bottom - top + 40), 0)
    ImageDraw.Draw(img).text((20 - left, 20 - top), WORD, font=font, fill=255)
    px = img.load()
    scale = sub_rows * sub_h / (bottom - top)
    sub_cols = math.ceil((right - left) * scale / sub_w)
    bits = set()
    for sy in range(sub_rows):
        y0 = 20 + int(sy * sub_h / scale)
        y1 = max(y0 + 1, 20 + int((sy + 1) * sub_h / scale))
        for sx in range(sub_cols):
            x0 = 20 + int(sx * sub_w / scale)
            x1 = max(x0 + 1, 20 + int((sx + 1) * sub_w / scale))
            total = hit = 0
            for y in range(y0, min(y1, img.height)):
                for x in range(x0, min(x1, img.width)):
                    total += 1
                    hit += px[x, y] >= 128
            if total and hit * 2 >= total:
                bits.add((sx, sy))
    return bits


def shadow_of(bits):
    """One cell right and half a row down."""
    return {(x + 2, y + 1) for x, y in bits}


def compose(letter, shadow):
    """Cells as (TL, TR, BL, BR) classes: L letter, S shadow, . nothing. A terminal cell holds
    two colours, so a cell that would need three drops its shadow quarters."""
    xs = [x for x, _ in letter | shadow]
    ys = [y for _, y in letter | shadow]
    cols = max(xs) // 2 + 1
    rows = max(ys) // 2 + 1
    dropped = 0
    grid = []
    for cy in range(rows):
        row = []
        for cx in range(cols):
            q = []
            for dy in (0, 1):
                for dx in (0, 1):
                    p = (2 * cx + dx, 2 * cy + dy)
                    q.append("L" if p in letter else "S" if p in shadow else ".")
            if len(set(q)) == 3:
                q = ["." if c == "S" else c for c in q]
                dropped += 1
            row.append(tuple(q))
        grid.append(row)
    if dropped:
        print(f"{dropped} cells dropped their shadow", file=sys.stderr)
    return grid


TOKEN = {"L": "{{c.accent}}", "S": "{{c.dim}}", ".": "transparent"}
PALETTE = {
    "dark": {"L": "#a7d91d", "S": "#3f403c", ".": "#0d0e0b", "X": "#71726e"},
    "light": {"L": "#4c7200", "S": "#bfc1bc", ".": "#f9fbf6", "X": "#858783"},
}
GLYPH = {
    (0, 0, 0, 0): " ",
    (1, 0, 0, 0): "▘",
    (0, 1, 0, 0): "▝",
    (0, 0, 1, 0): "▖",
    (0, 0, 0, 1): "▗",
    (1, 1, 0, 0): "▀",
    (0, 0, 1, 1): "▄",
    (1, 0, 1, 0): "▌",
    (0, 1, 0, 1): "▐",
    (1, 0, 0, 1): "▚",
    (0, 1, 1, 0): "▞",
    (1, 1, 1, 0): "▛",
    (1, 1, 0, 1): "▜",
    (1, 0, 1, 1): "▙",
    (0, 1, 1, 1): "▟",
    (1, 1, 1, 1): "█",
}


def css(q):
    tl, tr, bl, br = (TOKEN[c] for c in q)
    if tl == tr == bl == br:
        return "" if tl == "transparent" else f"background: {tl};"
    if tl == tr and bl == br:
        return f"background: linear-gradient({tl} 50%, {bl} 50%);"
    if tl == bl and tr == br:
        return f"background: linear-gradient(90deg, {tl} 50%, {tr} 50%);"
    return (
        f"background: linear-gradient(90deg, {tl} 50%, {tr} 50%) top / 100% 50% no-repeat, "
        f"linear-gradient(90deg, {bl} 50%, {br} 50%) bottom / 100% 50% no-repeat;"
    )


def cell_glyph(q):
    """Glyph, foreground class and background class of one cell."""
    classes = set(q)
    if classes == {"."}:
        return " ", None, None
    fg = "L" if "L" in classes else "S"
    bg = next((c for c in ("S", ".") if c in classes and c != fg), ".")
    return GLYPH[tuple(int(c == fg) for c in q)], fg, bg


def sgr(hexcolor, layer):
    r, g, b = int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16)
    return f"\x1b[{layer};2;{r};{g};{b}m"


def emit_grid(grid):
    cols, rows = len(grid[0]), len(grid)
    print(f"{VARIANT} {WORD} {cols}x{rows}", file=sys.stderr)
    if MODE == "grid":
        print(f"# halfblock {cols}x{rows}")
        for row in grid:
            print(
                "".join(
                    "a" if "L" in pair else "d" if "S" in pair else "."
                    for q in row
                    for pair in (q[:2], q[2:])
                )
            )
    elif MODE == "html":
        for row in grid:
            runs = []
            for q in row:
                style = css(q)
                if runs and runs[-1][0] == style:
                    runs[-1][1] += 1
                else:
                    runs.append([style, 1])
            parts = [
                " " * n if not style else f'<span style="{style}">{" " * n}</span>'
                for style, n in runs
            ]
            print("          <div>" + "".join(parts) + "</div>")
    elif MODE == "text":
        for row in grid:
            print("".join(cell_glyph(q)[0] for q in row).rstrip())
    else:
        pal = PALETTE[MODE.split("-")[1]]
        for row in grid:
            out = sgr(pal["."], 48)
            for q in row:
                glyph, fg, bg = cell_glyph(q)
                if fg is None:
                    out += sgr(pal["."], 48) + glyph
                else:
                    out += sgr(pal[fg], 38) + sgr(pal[bg], 48) + glyph
            print(out + "\x1b[0m")


# figlet "ANSI Shadow": blocks with their right and bottom edges extruded as double box lines.
# The extrusion follows one rule per empty cell from the letter cells to its left (l), above (u)
# and above-left (ul); applied to the face's own capitals it regenerates them glyph for glyph.
FIGLET = {
    "G": [" ██████╗ ", "██╔════╝ ", "██║  ███╗", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "B": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
}
FIGLET_LOWER = {  # letter cells only, eight columns: stems two wide, bars one column into a stem
    "g": [
        "........",
        "........",
        ".#######",
        "##....##",
        "##....##",
        ".#######",
        "......##",
        ".######.",
    ],
    "o": [
        "........",
        "........",
        ".######.",
        "##....##",
        "##....##",
        ".######.",
        "........",
        "........",
    ],
    "b": [
        "##......",
        "##......",
        "#######.",
        "##....##",
        "##....##",
        "#######.",
        "........",
        "........",
    ],
    "y": [
        "........",
        "........",
        "##....##",
        "##....##",
        "##....##",
        ".#######",
        "......##",
        ".######.",
    ],
}


def run_end(body, x, y):
    while (x + 1, y) in body:
        x += 1
    return x


def extrude(body, cols, rows):
    lines = []
    for y in range(rows + 1):
        line = ""
        for x in range(cols + 1):
            if (x, y) in body:
                line += "█"
                continue
            left, above, above_left = (x - 1, y) in body, (x, y - 1) in body, (x - 1, y - 1) in body
            if left and above:
                line += "╔"
            elif left:
                line += "║" if above_left else "╗"
            elif above:
                # a bar that overhangs the stem below it by one column is a rounded shoulder
                # and casts nothing there (the face's G); a stair step (its Y) still does
                shoulder = (x + 1, y) in body and run_end(body, x, y - 1) == run_end(body, x + 1, y)
                line += "═" if above_left else " " if shoulder else "╚"
            elif above_left:
                line += "╝"
            else:
                line += " "
        lines.append(line.rstrip())
    return lines


def figlet_lines():
    body = set()
    if LOWER:
        x0 = 0
        for ch in WORD:
            for y, row in enumerate(FIGLET_LOWER[ch]):
                body.update((x0 + x, y) for x, c in enumerate(row) if c == "#")
            x0 += 9  # eight columns of letter and one for the extrusion
        cols, rows = x0 - 1, ROWS
        return extrude(body, cols, rows)
    given = ["".join(FIGLET[ch][r] for ch in WORD).rstrip() for r in range(6)]
    for y, line in enumerate(given):
        body.update((x, y) for x, c in enumerate(line) if c == "█")
    lines = extrude(body, max(len(line) for line in given), 5)
    for expected, actual in zip(given, lines, strict=True):
        if expected != actual:
            print("figlet\n" + expected + "\nrule\n" + actual, file=sys.stderr)
    assert lines == given, "extrusion rule drifted from the figlet face"
    return lines


def emit_figlet():
    lines = figlet_lines()
    width = max(len(line) for line in lines)
    print(f"figlet {WORD} {width}x{len(lines)}", file=sys.stderr)
    for line in lines:
        if MODE == "html":
            parts, i = [], 0
            while i < len(line):
                j = i
                kind = "█" if line[i] == "█" else " " if line[i] == " " else "x"
                while j < len(line) and (
                    ("█" if line[j] == "█" else " " if line[j] == " " else "x") == kind
                ):
                    j += 1
                seg = line[i:j]
                if kind == "█":
                    parts.append(
                        f'<span style="background: {{{{c.accent}}}};">{" " * len(seg)}</span>'
                    )
                elif kind == " ":
                    parts.append(seg)
                else:
                    parts.append(f'<span style="color: {{{{c.overlay0}}}};">{seg}</span>')
                i = j
            print("          <div>" + "".join(parts) + "</div>")
        elif MODE == "text":
            print(line)
        else:
            pal = PALETTE[MODE.split("-")[1]]
            out = sgr(pal["."], 48)
            for ch in line:
                out += sgr(pal["L"] if ch == "█" else pal["X"], 38) + ch
            print(out + "\x1b[0m")


def emit_braille(bits):
    xs = [x for x, _ in bits]
    cols = max(xs) // 2 + 1
    print(f"braille {WORD} {cols}x{ROWS}", file=sys.stderr)
    # dot d (1..8) of a braille cell: 1 2 3 7 down the left column, 4 5 6 8 down the right
    BIT = {(0, 0): 0, (0, 1): 1, (0, 2): 2, (1, 0): 3, (1, 1): 4, (1, 2): 5, (0, 3): 6, (1, 3): 7}
    lines = []
    for cy in range(ROWS):
        line = ""
        for cx in range(cols):
            code = 0
            for (dx, dy), b in BIT.items():
                if (2 * cx + dx, 4 * cy + dy) in bits:
                    code |= 1 << b
            line += chr(0x2800 + code)
        lines.append(line)
    if MODE == "grid":
        print(f"# braille {cols}x{ROWS}")
        for line in lines:
            print(line)
    elif MODE == "html":
        w, h = cols * CW, ROWS * CH
        circles = "".join(
            f'<circle cx="{(x * CW / 2 + CW / 4):.1f}" cy="{(y * CH / 4 + CH / 8):.1f}" r="1.3"/>'
            for x, y in sorted(bits)
        )
        print(
            f'          <svg width="{w:.1f}" height="{h:.0f}" viewBox="0 0 {w:.1f} {h:.0f}" '
            f'style="display: block; fill: {{{{c.accent}}}};">{circles}</svg>'
        )
    elif MODE == "text":
        for line in lines:
            print(line)
    else:
        pal = PALETTE[MODE.split("-")[1]]
        for line in lines:
            print(sgr(pal["."], 48) + sgr(pal["L"], 38) + line + "\x1b[0m")


if VARIANT == "figlet":
    emit_figlet()
elif VARIANT == "braille":
    emit_braille(raster_bits(FONT, CW / 2, CH / 4, ROWS * 4))
else:
    if VARIANT.startswith("raster"):
        letter = raster_bits(FONT, CW / 2, CH / 2, ROWS * 2)
    else:
        letter = block_bits(chamfer=VARIANT.startswith("round"))
    shadow = shadow_of(letter) - letter if VARIANT.endswith("shadow") else set()
    emit_grid(compose(letter, shadow))
