import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'

const root = path.resolve('src')
const violationPattern = /var\(--[A-Za-z0-9_-]+,\s*(#[0-9a-fA-F]+)\)/g
const ignoredDirs = new Set(['node_modules', 'dist', 'coverage'])
const tokenLintExtensions = new Set(['.ts', '.tsx', '.js', '.jsx', '.css'])
const violations = []

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true })
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (!ignoredDirs.has(entry.name)) {
        await walk(path.join(dir, entry.name))
      }
      continue
    }
    if (entry.isFile() && tokenLintExtensions.has(path.extname(entry.name))) {
      await checkFile(path.join(dir, entry.name))
    }
  }
}

async function checkFile(filePath) {
  let text
  try {
    text = await readFile(filePath, 'utf8')
  } catch (error) {
    violations.push({
      filePath,
      line: 0,
      column: 0,
      text: `failed to read file: ${error instanceof Error ? error.message : String(error)}`,
    })
    return
  }

  const lines = text.split(/\r?\n/)
  for (const [lineIndex, line] of lines.entries()) {
    violationPattern.lastIndex = 0
    let match
    while ((match = violationPattern.exec(line)) !== null) {
      violations.push({
        filePath,
        line: lineIndex + 1,
        column: match.index + 1,
        text: match[0],
      })
    }
  }
}

await walk(root)

if (violations.length > 0) {
  for (const violation of violations) {
    const relPath = path.relative(process.cwd(), violation.filePath)
    console.error(`${relPath}:${violation.line}:${violation.column}: ${violation.text}`)
  }
  process.exit(1)
}
