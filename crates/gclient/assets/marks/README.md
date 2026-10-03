# Gobby terminal marks

These are the committed, theme-neutral source assets for the gclient mark and
wordmark. The goblin comes from [the product logo](../../../../web/public/logo.png).
The generators require Pillow through `uv run python`; the wordmark also requires
the macOS Helvetica Neue Condensed Black system font. Wordmark regeneration is
macOS-only. The chrome renderer will embed the committed text grids at build
time; no Python runs in gclient to draw them.

The mark has **three homes**: the splash screen, About dialog, and Empty tab.
It must not appear elsewhere in the client.

## Grid format

Each UTF-8 file has LF line endings, no trailing whitespace, a first line
`# halfblock <cols>x<rows>` or `# braille <cols>x<rows>`, optional further
`#` comment lines, and exactly `rows` data lines. A halfblock line has
`2*cols` role characters, upper then lower for each terminal cell.
A braille line has `cols` glyphs in U+2800–U+28FF; U+2800 is transparent.

| Role | Meaning | Theme role |
| --- | --- | --- |
| `a` | Goblin fill or wordmark letter | `accent` |
| `o` | Tablet | `overlay1` |
| `i` | Outline and lenses | `ink` |
| `g` | Glint | `glint` |
| `d` | Wordmark drop shadow | `dim` |
| `.` | Transparent | No paint |

The light theme's `ink` is the `line` token, the near-black of the menu
bar's underline.

The Empty tab uses the same goblin grid with a dim palette that keeps every
role distinct, glints included, so both eyes survive. In dark mode `a` maps
to `overlay0`, `o` to `dim`, `i` to the panel colour and `g` to `subtext0`.
In light mode `a` maps to `surface1`, `o` to `overlay0`, `i` to `subtext0`
and `g` to the panel colour. No separate dimmed grid is stored. The shadow grid places its `d` role one
cell to the right and half a cell below the letters.

## Regenerate

Run from the repository root. The 56×16 and 48×14 boxes preserve the source
image aspect; the generator crops them to 33×16 and 29×14 drawn marks.
`goblin-33x16.grid` is hand-corrected after generation so both eyes draw the
same cells (#23280); never regenerate it, and edit it by hand. Its `.ans`
previews show the generator's output. The other eleven commands rewrite the
committed files byte-identically.

```sh
uv run python crates/gclient/assets/marks/mask2.py web/public/logo.png 48 14 grid > crates/gclient/assets/marks/goblin-29x14.grid
uv run python crates/gclient/assets/marks/wordmark.py braille grid lower > crates/gclient/assets/marks/wordmark-braille-54x8.txt
uv run python crates/gclient/assets/marks/wordmark.py shadow grid lower > crates/gclient/assets/marks/wordmark-shadow-49x9.grid
uv run python crates/gclient/assets/marks/mask2.py web/public/logo.png 56 16 ansi-dark > crates/gclient/assets/marks/renders/goblin-33x16-dark.ans
uv run python crates/gclient/assets/marks/mask2.py web/public/logo.png 56 16 ansi-light > crates/gclient/assets/marks/renders/goblin-33x16-light.ans
uv run python crates/gclient/assets/marks/mask2.py web/public/logo.png 48 14 ansi-dark > crates/gclient/assets/marks/renders/goblin-29x14-dark.ans
uv run python crates/gclient/assets/marks/mask2.py web/public/logo.png 48 14 ansi-light > crates/gclient/assets/marks/renders/goblin-29x14-light.ans
uv run python crates/gclient/assets/marks/wordmark.py braille ansi-dark lower > crates/gclient/assets/marks/renders/wordmark-braille-dark.ans
uv run python crates/gclient/assets/marks/wordmark.py braille ansi-light lower > crates/gclient/assets/marks/renders/wordmark-braille-light.ans
uv run python crates/gclient/assets/marks/wordmark.py shadow ansi-dark lower > crates/gclient/assets/marks/renders/wordmark-shadow-dark.ans
uv run python crates/gclient/assets/marks/wordmark.py shadow ansi-light lower > crates/gclient/assets/marks/renders/wordmark-shadow-light.ans
```

The `.ans` files render the same four marks with 24-bit SGR colours for
terminal inspection. They are visual previews and are not read by gclient.
