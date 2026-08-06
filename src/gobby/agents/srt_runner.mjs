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
  // sandbox-runtime allocates its mux/TLS unix sockets under os.tmpdir(), and
  // the per-run managed-execution TMPDIR is too deep for sun_path (104 bytes
  // on macOS). Run the runner itself out of the short GOBBY_SRT_TMPDIR while
  // the provider child keeps the policy-allowed per-run TMPDIR.
  const providerTmpdir = process.env.TMPDIR
  const muxTmpdir = process.env.GOBBY_SRT_TMPDIR
  if (muxTmpdir) process.env.TMPDIR = muxTmpdir
  const rawSettings = JSON.parse(readFileSync(options.settingsPath, 'utf8'))
  const parsed = SandboxRuntimeConfigSchema.safeParse(rawSettings)
  if (!parsed.success) {
    throw new Error(`invalid SRT policy: ${parsed.error.message}`)
  }
  if (!SandboxManager.isSupportedPlatform()) {
    throw new Error(`SRT does not support platform ${process.platform}`)
  }

  let seenViolations = 0
  let unsubscribe = () => {}
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGWINCH']
  const signalHandlers = new Map()
  let outcome
  let failure
  try {
    await SandboxManager.initialize(parsed.data, undefined, true)
    unsubscribe = SandboxManager.getSandboxViolationStore().subscribe(violations => {
      seenViolations = appendViolations(options.violationsPath, violations, seenViolations)
    })

    const command = options.preflight ? [process.execPath, '--version'] : options.command
    if (command.length === 0) throw new Error('missing provider command after --')

    const commandText = command.map(shellQuote).join(' ')
    const wrapped = await SandboxManager.wrapWithSandboxArgv(
      commandText,
      undefined,
      undefined,
      undefined,
      process.cwd(),
    )
    const childEnv = { ...process.env, ...wrapped.env }
    delete childEnv.GOBBY_SRT_TMPDIR
    if (muxTmpdir && !('TMPDIR' in wrapped.env)) {
      if (providerTmpdir === undefined) delete childEnv.TMPDIR
      else childEnv.TMPDIR = providerTmpdir
    }
    const child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
      cwd: process.cwd(),
      env: childEnv,
      stdio: 'inherit',
    })

    for (const signal of signals) {
      const handler = () => {
        if (child.exitCode === null && child.signalCode === null) child.kill(signal)
      }
      signalHandlers.set(signal, handler)
      process.on(signal, handler)
    }
    outcome = await new Promise((resolve, reject) => {
      child.once('error', reject)
      child.once('exit', (code, signal) => {
        resolve({ code: code ?? 1, signal })
      })
    })
  } catch (error) {
    failure = error
  } finally {
    for (const [signal, handler] of signalHandlers) {
      try {
        process.off(signal, handler)
      } catch (error) {
        failure ??= error
      }
    }
    try {
      unsubscribe()
    } catch (error) {
      failure ??= error
    }
    try {
      seenViolations = appendViolations(
        options.violationsPath,
        SandboxManager.getSandboxViolationStore().getViolations(),
        seenViolations,
      )
    } catch (error) {
      failure ??= error
    }
    try {
      await SandboxManager.reset()
    } catch (error) {
      failure ??= error
    }
  }

  if (failure) throw failure
  if (!outcome) throw new Error('provider process completed without an outcome')
  if (outcome.signal) {
    process.kill(process.pid, outcome.signal)
    return 128
  }
  return outcome.code
}

try {
  process.exitCode = await main()
} catch (error) {
  console.error(`gobby-srt: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
