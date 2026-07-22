#!/usr/bin/env node

import { appendFileSync, chmodSync, readFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import process from 'node:process'

import {
  SandboxManager,
  SandboxRuntimeConfigSchema,
} from './node_modules/@anthropic-ai/sandbox-runtime/dist/index.js'

function parseArgs(argv) {
  const separator = argv.indexOf('--')
  const options = separator === -1 ? argv : argv.slice(0, separator)
  const command = separator === -1 ? [] : argv.slice(separator + 1)
  const valueAfter = name => {
    const index = options.indexOf(name)
    if (index === -1 || !options[index + 1]) throw new Error(`missing ${name}`)
    return options[index + 1]
  }
  return {
    settingsPath: valueAfter('--settings'),
    violationsPath: valueAfter('--violations'),
    preflight: options.includes('--preflight'),
    command,
  }
}

function shellQuote(value) {
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

function appendViolations(path, violations, start) {
  for (const violation of violations.slice(start)) {
    const line = JSON.stringify(violation, (_, value) =>
      typeof value === 'bigint' ? value.toString() : value,
    )
    appendFileSync(path, `${line}\n`, { encoding: 'utf8', mode: 0o600 })
  }
  chmodSync(path, 0o600)
  return violations.length
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  const rawSettings = JSON.parse(readFileSync(options.settingsPath, 'utf8'))
  const parsed = SandboxRuntimeConfigSchema.safeParse(rawSettings)
  if (!parsed.success) {
    throw new Error(`invalid SRT policy: ${parsed.error.message}`)
  }
  if (!SandboxManager.isSupportedPlatform()) {
    throw new Error(`SRT does not support platform ${process.platform}`)
  }

  await SandboxManager.initialize(parsed.data, undefined, true)
  let seenViolations = 0
  const unsubscribe = SandboxManager.getSandboxViolationStore().subscribe(violations => {
    seenViolations = appendViolations(options.violationsPath, violations, seenViolations)
  })

  const command = options.preflight
    ? [process.platform === 'win32' ? process.execPath : '/usr/bin/true']
    : options.command
  if (command.length === 0) throw new Error('missing provider command after --')
  if (options.preflight && process.platform === 'win32') command.push('--version')

  const commandText = command.map(shellQuote).join(' ')
  const wrapped = await SandboxManager.wrapWithSandboxArgv(
    commandText,
    undefined,
    undefined,
    undefined,
    process.cwd(),
  )
  const child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
    cwd: process.cwd(),
    env: { ...process.env, ...wrapped.env },
    stdio: 'inherit',
  })

  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGWINCH']
  const signalHandlers = new Map()
  for (const signal of signals) {
    const handler = () => {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal)
    }
    signalHandlers.set(signal, handler)
    process.on(signal, handler)
  }
  const outcome = await new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('exit', (code, signal) => {
      resolve({ code: code ?? 1, signal })
    })
  })
  for (const [signal, handler] of signalHandlers) process.off(signal, handler)
  unsubscribe()
  seenViolations = appendViolations(
    options.violationsPath,
    SandboxManager.getSandboxViolationStore().getViolations(),
    seenViolations,
  )
  await SandboxManager.reset()
  if (outcome.signal) {
    process.kill(process.pid, outcome.signal)
    return 128
  }
  return outcome.code
}

try {
  process.exitCode = await main()
} catch (error) {
  try {
    await SandboxManager.reset()
  } catch {
    // Preserve the original fail-closed error.
  }
  console.error(`gobby-srt: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
