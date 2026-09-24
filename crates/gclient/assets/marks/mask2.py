"""Logo PNG -> half-block goblin. Outline pixels become holes; green, grey and white keep their class.

python3 mask2.py <png> <cols> <rows> [text|html|ansi-dark|ansi-light|grid]
"""

import sys
from collections import Counter, deque

from PIL import Image, ImageFilter

path, cols, rows = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
img = Image.open(path).convert("RGBA")
w, h = img.size
px = img.load()


def cls(p):
    r, g, b, a = p
    if a < 128:
        return "."
    m = max(r, g, b)
    if m < 60:
        return "K"
    if g > 90 and g > r + 20 and g > b + 40:
        return "G"
    if r > 200 and g > 200 and b > 200:
        return "W"
    if abs(r - g) < 20 and abs(g - b) < 20:
        return "E"
    return "P"


# the logo's lenses are solid black discs; the mark draws them as white eyeballs with black pupils.
# Solid black is eroded twice: past the frame thickness it is eyeball, past the pupil radius it is pupil.
solid = Image.new("L", (w, h), 0)
sp = solid.load()
for y in range(h):
    for x in range(w):
        if px[x, y][3] >= 128 and cls(px[x, y]) in ("K", "W"):
            sp[x, y] = 255
eroded = solid
for i in range(42):
    eroded = eroded.filter(ImageFilter.MinFilter(3))
    if i == 19:
        eye_white = eroded.load()
pupil = eroded.load()


def pixel_class(x, y):
    if pupil[x, y]:
        return "K"
    if eye_white[x, y]:
        return "W"
    return cls(px[x, y])


def body_px(x, y):
    return px[x, y][3] >= 128 and cls(px[x, y]) in ("G", "E", "K")


def flood(start, inside):
    seen = set(start)
    q = deque(start)
    while q:
        x, y = q.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and inside(nx, ny):
                seen.add((nx, ny))
                q.append((nx, ny))
    return seen


# largest component of green, grey and outline pixels: the goblin and its tablet, not the halo dots
seen = set()
best = set()
for sy in range(0, h, 2):
    for sx in range(0, w, 2):
        if body_px(sx, sy) and (sx, sy) not in seen:
            comp = flood([(sx, sy)], body_px)
            seen |= comp
            if len(comp) > len(best):
                best = comp
# everything the border can reach without crossing the body is outside; the rest (lens highlights) is enclosed
border = [(x, y) for x in range(w) for y in (0, h - 1)] + [
    (x, y) for y in range(h) for x in (0, w - 1)
]
outside = flood([p for p in border if p not in best], lambda x, y: (x, y) not in best)
keep = [
    [(x, y) in best or ((x, y) not in outside and px[x, y][3] >= 128) for x in range(w)]
    for y in range(h)
]
best = [(x, y) for y in range(h) for x in range(w) if keep[y][x]]
xs = [x for x, _ in best]
ys = [y for _, y in best]
x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
bw, bh = x1 - x0, y1 - y0
print(f"{path}: blob {bw}x{bh}", file=sys.stderr)

sub_rows = rows * 2
scale = min(cols * 8.4 / bw, sub_rows * 10 / bh)
gw, gh = int(bw * scale / 8.4), int(bh * scale / 10)
ox, oy = (cols - gw) // 2, (sub_rows - gh) // 2


def cell(cx, cy):
    """Class of one subcell: '.', or the majority non-outline class when fill covers half."""
    if not (ox <= cx < ox + gw and oy <= cy < oy + gh):
        return "."
    sx0 = x0 + int((cx - ox) * 8.4 / scale)
    sx1 = x0 + max(sx0 - x0 + 1, int((cx - ox + 1) * 8.4 / scale))
    sy0 = y0 + int((cy - oy) * 10 / scale)
    sy1 = y0 + max(sy0 - y0 + 1, int((cy - oy + 1) * 10 / scale))
    total = 0
    cnt = Counter()
    for y in range(sy0, min(sy1, y1)):
        for x in range(sx0, min(sx1, x1)):
            total += 1
            if keep[y][x]:
                cnt[pixel_class(x, y)] += 1
    fill = cnt["G"] + cnt["E"] + cnt["W"] + cnt["P"]
    if not total:
        return "."
    if fill * 2 < total:
        # outline: ink in light mode, the panel colour in dark mode
        return "K" if (fill + cnt["K"]) * 2 >= total else "."
    return max(("G", "E", "W", "P"), key=lambda k: cnt[k])


MODE = sys.argv[4] if len(sys.argv) > 4 else "text"
TOKEN = {
    "G": "{{c.accent}}",
    "E": "{{c.overlay1}}",
    "W": "{{c.glint}}",
    "P": "{{c.accent}}",
    "K": "{{c.ink}}",
}
PALETTE = {
    "dark": {
        "G": "#a7d91d",
        "E": "#858783",
        "W": "#e3e5e1",
        "P": "#a7d91d",
        "K": "#0d0e0b",
        ".": "#0d0e0b",
    },
    "light": {
        "G": "#4c7200",
        "E": "#686966",
        "W": "#f9fbf6",
        "P": "#4c7200",
        "K": "#151714",
        ".": "#f9fbf6",
    },
}
grid = [[(cell(c, 2 * r), cell(c, 2 * r + 1)) for c in range(cols)] for r in range(rows)]
# the mark is narrower than its box; emit only its own columns so layouts centre the visible shape
used = [c for c in range(cols) if any(grid[r][c] != (".", ".") for r in range(rows))]
grid = [row[used[0] : used[-1] + 1] for row in grid]
print(f"{used[-1] + 1 - used[0]}x{rows} mark from a {cols}x{rows} box", file=sys.stderr)


def runs_of(row):
    runs = []
    for t, b in row:
        if t == "." and b == ".":
            glyph, fg, bg = " ", None, None
        elif t == b:
            glyph, fg, bg = "█", t, None
        elif t == ".":
            glyph, fg, bg = "▄", b, None
        elif b == ".":
            glyph, fg, bg = "▀", t, None
        else:
            glyph, fg, bg = "▀", t, b
        if runs and runs[-1][1] == fg and runs[-1][2] == bg:
            runs[-1][0] += glyph
        else:
            runs.append([glyph, fg, bg])
    return runs


def sgr(hexcolor, layer):
    r, g, b = int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16)
    return f"\x1b[{layer};2;{r};{g};{b}m"


if MODE == "grid":
    roles = {"G": "a", "P": "a", "E": "o", "W": "g", "K": "i", ".": "."}
    print(f"# halfblock {len(grid[0])}x{rows}")
    for row in grid:
        print("".join(roles[top] + roles[bottom] for top, bottom in row))
elif MODE == "text":

    def on(value):
        return value not in (".", "K")

    for row in grid:
        print(
            "".join(
                "█" if on(t) and on(b) else "▀" if on(t) else "▄" if on(b) else " " for t, b in row
            ).rstrip()
        )
    print(file=sys.stderr)
    for row in grid:
        print(
            "".join(t if t != "." else b.lower() if b != "." else " " for t, b in row).rstrip(),
            file=sys.stderr,
        )
elif MODE == "html":
    # cells are painted as backgrounds (a half cell is a two-stop gradient), which is how a
    # terminal fills block glyphs: edge to edge, no font gaps
    for row in grid:
        runs = []
        for t, b in row:
            pair = (TOKEN.get(t), TOKEN.get(b))
            if runs and runs[-1][0] == pair:
                runs[-1][1] += 1
            else:
                runs.append([pair, 1])
        parts = []
        for (top, bottom), n in runs:
            if top is None and bottom is None:
                parts.append(" " * n)
            elif top == bottom:
                parts.append(f'<span style="background: {top};">{" " * n}</span>')
            else:
                parts.append(
                    f'<span style="background: linear-gradient({top or "transparent"} 50%, {bottom or "transparent"} 50%);">{" " * n}</span>'
                )
        print("          <div>" + "".join(parts) + "</div>")
else:
    pal = PALETTE[MODE.split("-")[1]]
    for row in grid:
        out = sgr(pal["."], 48)
        for glyphs, fg, bg in runs_of(row):
            if fg is None:
                out += sgr(pal["."], 48) + glyphs
            else:
                out += sgr(pal[fg], 38) + sgr(pal[bg] if bg else pal["."], 48) + glyphs
        print(out + "\x1b[0m")
