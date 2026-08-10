import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC_ROOT = join(process.cwd(), 'src')
const AUTHORITY = 'api/config.ts'

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    if (statSync(path).isDirectory()) {
      return name === '__tests__' ? [] : sourceFiles(path)
    }
    return /\.(?:ts|tsx)$/.test(name) ? [path] : []
  })
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
}

describe('browser configuration authority', () => {
  it('web_has_one_config_authority', () => {
    const violations: string[] = []
    for (const path of sourceFiles(SRC_ROOT)) {
      const relativePath = relative(SRC_ROOT, path)
      if (relativePath === AUTHORITY) continue
      const source = stripComments(readFileSync(path, 'utf8'))
      if (/fetch\s*\([\s\S]{0,300}\/api\/config\//.test(source)) {
        violations.push(`${relativePath}: direct configuration fetch`)
      }
      if (/\/api\/config\/(?:ui-settings|values\/reset|launch-defaults?)/.test(source)) {
        violations.push(`${relativePath}: specialized configuration endpoint`)
      }
    }

    const authority = readFileSync(join(SRC_ROOT, AUTHORITY), 'utf8')
    expect(authority).toContain("method: 'PATCH'")
    expect(authority).toContain('expected_revision: snapshot.revision')
    expect(violations).toEqual([])
  })
})
