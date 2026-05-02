import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const sourceRoot = join(process.cwd(), 'src')
const selfPath = join(sourceRoot, '__tests__/test_legacy_symbols_removed.test.ts')
const legacyTerms = [
  ['getTask', 'Bucket'].join(''),
  ['Task', 'Bucket'].join(''),
  ['TASK', '_BUCKET_'].join(''),
  ['lifecycle', '_stage'].join(''),
]
const legacyPattern = new RegExp(legacyTerms.join('|'))

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const path = join(dir, entry)
    if (path === selfPath) return []
    if (statSync(path).isDirectory()) return sourceFiles(path)
    if (!/\.(ts|tsx)$/.test(path)) return []
    return [path]
  })
}

function legacyMatches() {
  return sourceFiles(sourceRoot).flatMap(file => {
    const source = readFileSync(file, 'utf8')
    return legacyPattern.test(source) ? [relative(process.cwd(), file)] : []
  })
}

describe('Legacy task-state symbols are removed from web source', () => {
  it('test_no_task_bucket_imports', () => {
    const offenders = legacyMatches().filter(file => {
      const source = readFileSync(join(process.cwd(), file), 'utf8')
      return /getTaskBucket|TaskBucket|TASK_BUCKET_/.test(source)
    })

    expect(offenders).toEqual([])
  })

  it('test_no_task_bucket_imports_in_web_src', () => {
    const offenders = legacyMatches().filter(file => {
      const source = readFileSync(join(process.cwd(), file), 'utf8')
      return /getTaskBucket|TaskBucket|TASK_BUCKET_/.test(source)
    })

    expect(offenders).toEqual([])
  })

  it('test_no_lifecycle_stage_reads_in_web_src', () => {
    const offenders = legacyMatches().filter(file => {
      const source = readFileSync(join(process.cwd(), file), 'utf8')
      return new RegExp(['lifecycle', '_stage'].join('')).test(source)
    })

    expect(offenders).toEqual([])
  })

  it('test_audit_grep_returns_zero_matches', () => {
    expect(legacyMatches()).toEqual([])
  })
})
