import type { CellData } from '@wterm/core'
import type { GhosttyCore } from '@wterm/ghostty'

// ghostty-vt resolves ANSI palette SGRs (30-37/90-97, 38;5/48;5 with N < 16)
// to concrete RGB from its built-in Tomorrow Night base-16 palette before
// cells leave the WASM core, so the renderer never sees palette indexes and
// the --term-color-N theme variables set by applyGobbyTheme go unused. The
// built-in palette is fixed, so map those exact RGB values back to their
// index and let the renderer emit var(--term-color-N). Truecolor output that
// exactly matches one of these 16 values is indistinguishable from palette
// output and gets themed too.
const ANSI_INDEX_BY_RGB = new Map<number, number>([
  [0x1d1f21, 0],
  [0xcc6666, 1],
  [0xb5bd68, 2],
  [0xf0c674, 3],
  [0x81a2be, 4],
  [0xb294bb, 5],
  [0x8abeb7, 6],
  [0xc5c8c6, 7],
  [0x666666, 8],
  [0xd54e53, 9],
  [0xb9ca4a, 10],
  [0xe7c547, 11],
  [0x7aa6da, 12],
  [0xc397d8, 13],
  [0x70c0b1, 14],
  [0xeaeaea, 15],
])

export function remapAnsiCell(cell: CellData): CellData {
  const fgIdx =
    cell.fgRgb === undefined ? undefined : ANSI_INDEX_BY_RGB.get(cell.fgRgb)
  const bgIdx =
    cell.bgRgb === undefined ? undefined : ANSI_INDEX_BY_RGB.get(cell.bgRgb)
  if (fgIdx === undefined && bgIdx === undefined) return cell
  const next = { ...cell }
  if (fgIdx !== undefined) {
    next.fg = fgIdx
    delete next.fgRgb
  }
  if (bgIdx !== undefined) {
    next.bg = bgIdx
    delete next.bgRgb
  }
  return next
}

export function withGobbyAnsiPalette(core: GhosttyCore): GhosttyCore {
  const getCell = core.getCell.bind(core)
  const getScrollbackCell = core.getScrollbackCell.bind(core)
  core.getCell = (row: number, col: number) => remapAnsiCell(getCell(row, col))
  core.getScrollbackCell = (offset: number, col: number) =>
    remapAnsiCell(getScrollbackCell(offset, col))
  return core
}
