import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const packageRoot = join(__dirname, '..')
const tailwindRoot = join(packageRoot, 'node_modules', '@tailwindcss', 'node')
const packageJsonPath = join(tailwindRoot, 'package.json')
const targetVersion = '4.3.0'
const patchMarker = 'gobby-tailwind-node-dep0205-patch'
const patchMarkerComment = `/* ${patchMarker} */\n`

function readText(path) {
  return readFileSync(path, 'utf8')
}

function patchFile(path, original, replacement) {
  const source = readText(path)
  if (source.includes(patchMarker)) {
    return 'already-patched'
  }
  if (source.includes(replacement)) {
    writeFileSync(path, `${patchMarkerComment}${source}`)
    return 'marker-added'
  }
  if (!source.includes(original)) {
    throw new Error(`Unexpected @tailwindcss/node contents in ${path}`)
  }
  writeFileSync(path, `${patchMarkerComment}${source.replace(original, replacement)}`)
  return 'patched'
}

let packageJson
try {
  packageJson = JSON.parse(readText(packageJsonPath))
} catch (error) {
  if (error instanceof SyntaxError) {
    console.warn(
      `Skipping @tailwindcss/node DEP0205 patch because ${packageJsonPath} is malformed: ${error.message}`,
    )
    process.exit(0)
  }
  throw error
}

if (packageJson.version !== targetVersion) {
  console.log(
    `Skipping @tailwindcss/node DEP0205 patch for version ${packageJson.version}; expected ${targetVersion}.`,
  )
  process.exit(0)
}

const cjsOriginal =
  'process.versions.bun||_t.register?.((0,Dt.pathToFileURL)(require.resolve("@tailwindcss/node/esm-cache-loader")));'
const cjsReplacement =
  'if(!process.versions.bun)if(_t.registerHooks){let e=(0,Dt.pathToFileURL)(require.resolve("@tailwindcss/node/esm-cache-loader")).href;_t.registerHooks({resolve(r,t,i){let o=i(r,t);if(o.url===e||_t.isBuiltin?.(o.url)||!t.parentURL)return o;let l=new URL(t.parentURL).searchParams.get("id");if(l===null)return o;let n=new URL(o.url);return n.searchParams.set("id",l),{...o,url:`${n}`}}})}else _t.register?.((0,Dt.pathToFileURL)(require.resolve("@tailwindcss/node/esm-cache-loader")));'

const esmOriginal =
  'if(!process.versions.bun){let e=fe.createRequire(import.meta.url);fe.register?.(Xr(e.resolve("@tailwindcss/node/esm-cache-loader")))}'
const esmReplacement =
  'if(!process.versions.bun){let e=fe.createRequire(import.meta.url),r=Xr(e.resolve("@tailwindcss/node/esm-cache-loader")).href;if(fe.registerHooks){fe.registerHooks({resolve(t,i,o){let l=o(t,i);if(l.url===r||fe.isBuiltin(l.url)||!i.parentURL)return l;let n=new URL(i.parentURL).searchParams.get("id");if(n===null)return l;let s=new URL(l.url);return s.searchParams.set("id",n),{...l,url:`${s}`}}})}else fe.register?.(Xr(e.resolve("@tailwindcss/node/esm-cache-loader")))}'

const results = [
  patchFile(join(tailwindRoot, 'dist', 'index.js'), cjsOriginal, cjsReplacement),
  patchFile(join(tailwindRoot, 'dist', 'index.mjs'), esmOriginal, esmReplacement),
]

console.log(`@tailwindcss/node DEP0205 patch: ${results.join(', ')}`)
