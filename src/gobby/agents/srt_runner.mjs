#!/usr/bin/env node

import { appendFileSync, chmodSync, readFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import process from 'node:process'
import { getPriority, setPriority } from 'node:os'

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

// The store keeps only its last 100 violations, so its array length stops
// growing once the tail is full. Its monotonic total is the cursor instead.
function appendViolations(path, store, recorded) {
  const total = store.getTotalCount()
  const violations = store.getViolations()
  const fresh = Math.min(total - recorded, violations.length)
  for (const violation of fresh > 0 ? violations.slice(-fresh) : []) {
    const line = JSON.stringify(violation, (_, value) =>
      typeof value === 'bigint' ? value.toString() : value,
    )
    appendFileSync(path, `${line}\n`, { encoding: 'utf8', mode: 0o600 })
  }
  chmodSync(path, 0o600)
  return total
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  // sandbox-runtime allocates its mux/TLS unix sockets under os.tmpdir(), and
  // the per-run managed-execution TMPDIR is too deep for sun_path (104 bytes
  // on macOS). Run the runner itself out of the short GOBBY_SRT_TMPDIR while
  // the provider child keeps the policy-allowed per-run TMPDIR.
  const providerTmpdir = process.env.TMPDIR
  const providerClaudeTmpdir = process.env.CLAUDE_CODE_TMPDIR
  const muxTmpdir = process.env.GOBBY_SRT_TMPDIR
  if (muxTmpdir) {
    process.env.TMPDIR = muxTmpdir
    // SRT embeds its temp selector in the shell/bwrap command as well as env.
    // Point it at the managed directory already allowed by this run's policy.
    if (providerTmpdir !== undefined) process.env.CLAUDE_CODE_TMPDIR = providerTmpdir
  }
  const rawSettings = JSON.parse(readFileSync(options.settingsPath, 'utf8'))
  const parsed = SandboxRuntimeConfigSchema.safeParse(rawSettings)
  if (!parsed.success) {
    throw new Error(`invalid SRT policy: ${parsed.error.message}`)
  }
  if (!SandboxManager.isSupportedPlatform()) {
    throw new Error(`SRT does not support platform ${process.platform}`)
  }

  // Set priority on the host: Seatbelt can deny setpriority after sandbox entry.
  // Every provider/tool child inherits this, and a failure must prevent launch.
  setPriority(0, 19)
  if (getPriority(0) !== 19) throw new Error('failed to set SRT runner niceness to 19')

  let seenViolations = 0
  let unsubscribe = () => {}
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGWINCH']
  const signalHandlers = new Map()
  let outcome
  let failure
  try {
    await SandboxManager.initialize(parsed.data, undefined, true)
    const store = SandboxManager.getSandboxViolationStore()
    unsubscribe = store.subscribe(() => {
      seenViolations = appendViolations(options.violationsPath, store, seenViolations)
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
    if (muxTmpdir) {
      if (providerTmpdir === undefined) delete childEnv.TMPDIR
      else childEnv.TMPDIR = providerTmpdir
      if (providerClaudeTmpdir === undefined) delete childEnv.CLAUDE_CODE_TMPDIR
      else childEnv.CLAUDE_CODE_TMPDIR = providerClaudeTmpdir
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
        SandboxManager.getSandboxViolationStore(),
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
