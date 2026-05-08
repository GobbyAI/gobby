import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const stylesDir = join(process.cwd(), 'src/styles')
const lifecycleStylesheet = join(stylesDir, 'lifecycle-board.css')
const rawColorPattern =
  /#[0-9a-fA-F]{3,8}\b|(?:rgb|rgba|hsl|hsla)\(\s*\d+(?:\s|,|\))/

function stylesheetFiles(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) return stylesheetFiles(path)
    return /lifecycle-board.*\.css$/.test(entry) ? [path] : []
  })
}

describe('Lifecycle board stylesheet token lint', () => {
  it('test_no_raw_color_literals', () => {
    expect(existsSync(lifecycleStylesheet)).toBe(true)

    const offenders = stylesheetFiles(stylesDir).flatMap(file => {
      const lines = readFileSync(file, 'utf8').split('\n')
      return lines.flatMap((line, index) => {
        const withoutInlineComments = line.replace(/\/\*.*?\*\//g, '')
        if (!rawColorPattern.test(withoutInlineComments)) return []
        return [`${relative(process.cwd(), file)}:${index + 1}`]
      })
    })

    expect(offenders).toEqual([])
  })
})
