// eslint-plugin-jsx-a11y 6.10.2 calls minimatch's legacy default export.
// minimatch >=10.0.3 fixes GHSA-mh99-v99m-4gvg and exposes the matcher as a
// named export, so adapt the one plugin call site until upstream supports it.
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const pluginRoot = join(scriptDir, '..', 'node_modules', 'eslint-plugin-jsx-a11y')
const packageJsonPath = join(pluginRoot, 'package.json')
const targetPath = join(pluginRoot, 'lib', 'util', 'mayContainChildComponent.js')
const targetVersion = '6.10.2'
const marker = 'gobby-jsx-a11y-minimatch-compat'
const original = '(0, _minimatch["default"])(elementType'
const previousReplacement =
  '(0, _minimatch["default"].minimatch)(elementType'
const replacement = '(0, _minimatch.minimatch)(elementType'

function readText(path) {
  return readFileSync(path, 'utf8')
}

let packageJson
try {
  packageJson = JSON.parse(readText(packageJsonPath))
} catch (error) {
  if (error?.code === 'ENOENT') {
    console.warn(
      `Skipping eslint-plugin-jsx-a11y minimatch patch because ${packageJsonPath} was not found.`,
    )
    process.exit(0)
  }
  throw error
}

if (packageJson.version !== targetVersion) {
  console.log(
    `Skipping eslint-plugin-jsx-a11y minimatch patch for version ${packageJson.version}; expected ${targetVersion}.`,
  )
  process.exit(0)
}

const source = readText(targetPath)
if (source.includes(marker)) {
  if (source.includes(previousReplacement)) {
    writeFileSync(
      targetPath,
      source.replace(previousReplacement, replacement),
    )
    console.log('eslint-plugin-jsx-a11y minimatch patch: updated')
    process.exit(0)
  }
  if (!source.includes(replacement)) {
    throw new Error(`Unexpected patched eslint-plugin-jsx-a11y contents in ${targetPath}`)
  }
  console.log('eslint-plugin-jsx-a11y minimatch patch: already-patched')
  process.exit(0)
}
if (!source.includes(original)) {
  throw new Error(`Unexpected eslint-plugin-jsx-a11y contents in ${targetPath}`)
}

writeFileSync(
  targetPath,
  `/* ${marker} */\n${source.replace(original, replacement)}`,
)
console.log('eslint-plugin-jsx-a11y minimatch patch: patched')
