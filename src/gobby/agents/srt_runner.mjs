#!/usr/bin/env node

import { appendFileSync, chmodSync, readFileSync, unlinkSync, writeSync, closeSync, openSync, fchmodSync, fstatSync, renameSync, constants } from 'node:fs'
import { createHash } from 'node:crypto'
import { spawn, execFileSync } from 'node:child_process'
import { createConnection, createServer } from 'node:net'
import { EventEmitter } from 'node:events'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import process from 'node:process'
import { getPriority, setPriority } from 'node:os'

import {
  SandboxManager,
  SandboxRuntimeConfigSchema,
} from './node_modules/@anthropic-ai/sandbox-runtime/dist/index.js'

// This host process outlives daemon restarts. Route complete eventMessage
// records by SRT's random session suffix, never by a provider PID.
const runnerIdentity = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex')

async function monitor(socketPath, readyFd) {
  setPriority(0, 19)
  const clients = new Map()
  let logProcess
  let stopping = false
  let listening = false
  let streamReady = false
  let idleTimer
  let unparsed = 0
  let journal
  let journalBytes = 0
  let sequence = 0
  let unrelated = 0
  const diagnosticPath = socketPath + '.jsonl'
  const openJournal = () => {
    journal = openSync(diagnosticPath, constants.O_APPEND | constants.O_CREAT | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600)
    fchmodSync(journal, 0o600)
    journalBytes = fstatSync(journal).size
  }
  const diagnostic = (line, error) => {
    const record = JSON.stringify({ unparsed: ++unparsed, error: error.message, record: line.slice(0, 8192) }) + '\n'
    try {
      if (journalBytes + Buffer.byteLength(record) > 1024 * 1024) {
        closeSync(journal)
        journal = undefined
        renameSync(diagnosticPath, diagnosticPath + '.1')
        openJournal()
      }
      appendFileSync(journal, record)
      journalBytes += Buffer.byteLength(record)
    } catch (failure) {
      console.error(`gobby-srt monitor diagnostics failed: ${failure.message}`)
    }
    if (unparsed === 1 || unparsed % 1000 === 0) {
      console.error(`gobby-srt monitor: ${unparsed} malformed records; ${error.message}`)
    }
  }
  const savePending = state => {
    if (!state.pending.size) return
    const path = socketPath + '.' + state.suffix + '.failed.jsonl'
    const fd = openSync(path, constants.O_APPEND | constants.O_CREAT | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600)
    try {
      fchmodSync(fd, 0o600)
      appendFileSync(fd, [...state.pending.values()].join(''))
      state.pending.clear()
      state.bytes = 0
    } finally { closeSync(fd) }
  }
  const finishConsumer = state => {
    if (state.finished) return
    state.finished = true
    try { savePending(state) } catch (error) {
      console.error(`gobby-srt monitor pending record save failed: ${error.message}`)
    }
    if (!state.done && state.providerPid !== null) {
      try { stopProviderTree(state.providerPid, state.providerIdentity) } catch (error) {
        console.error(`gobby-srt monitor provider cleanup failed: ${error.message}`)
      }
    }
  }
  const armIdle = () => {
    if (clients.size === 0 && !stopping) idleTimer = setTimeout(stop, 30000)
  }
  const acknowledge = socket => socket.write(JSON.stringify({ ready: true, identity: runnerIdentity }) + '\n')
  const markReady = () => {
    if (streamReady) return
    streamReady = true
    if (readyFd !== undefined) { writeSync(readyFd, 'ready\n'); closeSync(readyFd) }
    for (const [socket, state] of clients) if (state.suffix !== null) acknowledge(socket)
    armIdle()
  }
  const stop = () => {
    if (stopping) return
    stopping = true
    console.error(`gobby-srt monitor stopped: ${unrelated} unrelated, ${unparsed} malformed records`)
    clearTimeout(idleTimer)
    for (const socket of clients.keys()) socket.destroy()
    server.close()
    logProcess?.kill('SIGTERM')
    if (journal !== undefined) closeSync(journal)
    if (listening) { try { unlinkSync(socketPath) } catch {} }
  }
  const server = createServer(socket => {
    clearTimeout(idleTimer)
    const state = { suffix: null, runnerPid: null, providerPid: null, providerIdentity: null, done: false, finished: false, pending: new Map(), bytes: 0 }
    clients.set(socket, state)
    socket.setTimeout(5000, () => socket.destroy())
    const lines = createInterface({ input: socket, crlfDelay: Infinity })
    lines.on('line', line => {
      try {
        const request = JSON.parse(line)
        if (state.done || state.finished) throw new Error('closed monitor subscription')
        if (state.suffix === null && typeof request.suffix === 'string' && /^_[a-z0-9]+_SBX$/.test(request.suffix)) {
          state.suffix = request.suffix
          if (request.runnerPid !== undefined) {
            if (!Number.isSafeInteger(request.runnerPid) || request.runnerPid <= 1) throw new Error('invalid runner PID')
            state.runnerPid = request.runnerPid
          }
          socket.setTimeout(0)
          if (streamReady) acknowledge(socket)
        } else if (Number.isSafeInteger(request.ack) && state.pending.has(request.ack)) {
          state.bytes -= Buffer.byteLength(state.pending.get(request.ack))
          state.pending.delete(request.ack)
        } else if (Number.isSafeInteger(request.providerPid) && request.providerPid > 1 && state.providerPid === null) {
          if (state.runnerPid === null) throw new Error('provider registration needs its runner')
          state.providerIdentity = captureProviderIdentity(request.providerPid, state.runnerPid)
          state.providerPid = request.providerPid
          socket.write(JSON.stringify({ providerReady: true, identity: state.providerIdentity }) + '\n')
        } else if (request.done === true) {
          state.done = true
        } else throw new Error('invalid monitor request')
      } catch { socket.destroy() }
    })
    socket.on('error', () => socket.destroy())
    socket.on('close', () => {
      clients.delete(socket)
      lines.close()
      // This also stops a provider whose runner was killed or SIGSTOPped and
      // therefore cannot process its own disconnected-transport callback.
      finishConsumer(state)
      armIdle()
    })
  })
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(socketPath, resolve)
    })
    listening = true
    openJournal()
    chmodSync(socketPath, 0o600)
    logProcess = spawn('/usr/bin/log', [
      'stream', '--predicate', 'eventMessage ENDSWITH "_SBX"', '--style', 'ndjson',
    ], { stdio: ['ignore', 'pipe', 'inherit'] })
    const lines = createInterface({ input: logProcess.stdout, crlfDelay: Infinity })
    lines.on('line', line => {
      if (!line.trim()) return
      if (line.startsWith('Filtering the log data')) { markReady(); return }
      try {
        const event = JSON.parse(line)
        if (typeof event.eventMessage !== 'string') throw new Error('missing eventMessage')
        markReady()
        const message = event.eventMessage
        const suffix = message.match(/_[a-z0-9]+_SBX$/)?.[0]
        const id = ++sequence
        const record = JSON.stringify({ id, message }) + '\n'
        let matched = false
        for (const [socket, state] of clients) {
          if (!socket.destroyed && state.suffix === suffix) {
            matched = true
            state.pending.set(id, record)
            state.bytes += Buffer.byteLength(record)
            socket.write(record)
            if (state.bytes > 1024 * 1024) {
              console.error(`gobby-srt monitor: slow consumer ${suffix}; closing its subscription`)
              finishConsumer(state)
              socket.destroy()
            }
          }
        }
        if (!matched) unrelated++
      } catch (error) {
        diagnostic(line, error)
      }
    })
    await new Promise((resolve, reject) => {
      logProcess.once('error', reject)
      // close follows stdout drain; exit can arrive while the final records
      // are still buffered in the pipe.
      logProcess.once('close', (code, signal) => {
        if (!stopping) reject(new Error(`shared log stream exited: ${code ?? signal}`))
        else resolve()
      })
      for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.once(signal, stop)
    })
  } finally { stop() }
}

function sharedTransport(socketPath, suffix, failed) {
  const transport = new EventEmitter()
  transport.stdout = new EventEmitter()
  transport.stderr = new EventEmitter()
  const socket = createConnection(socketPath)
  const lines = createInterface({ input: socket, crlfDelay: Infinity })
  let stopped = false
  let ready = false
  let rejectReady
  let rejectRegistration
  transport.ready = new Promise((resolve, reject) => {
    rejectReady = reject
    const timer = setTimeout(() => fail(new Error('shared monitor readiness timed out')), 5000)
    transport.once('ready', () => { clearTimeout(timer); resolve() })
    transport.once('failed', () => clearTimeout(timer))
  })
  const fail = error => {
    if (stopped) return
    stopped = true
    transport.emit('failed')
    rejectReady(error)
    rejectRegistration?.(error)
    socket.destroy()
    failed(error)
  }
  socket.once('connect', () => socket.write(JSON.stringify({ suffix, runnerPid: process.pid }) + '\n'))
  lines.on('line', line => {
    try {
      const record = JSON.parse(line)
      if (!ready && record.ready === true && record.identity === runnerIdentity) {
        ready = true
        transport.emit('ready')
      } else if (ready && record.providerReady === true && record.identity && typeof record.identity.started === 'string' && Number.isSafeInteger(record.identity.group)) {
        transport.emit('providerReady', record.identity)
      } else if (ready && Number.isSafeInteger(record.id) && record.id > 0 && typeof record.message === 'string' && record.message.endsWith(suffix)) {
        transport.stdout.emit('data', Buffer.from(record.message))
        // Upstream processing and Gobby's append are synchronous. Ack only
        // after they finish; a lost consumer leaves its unacked tail durable.
        socket.write(JSON.stringify({ ack: record.id }) + '\n')
      } else throw new Error('invalid shared monitor record')
    } catch (error) { fail(error) }
  })
  socket.once('error', fail)
  socket.once('close', () => fail(new Error('shared violation monitor disconnected')))
  transport.providerStarted = pid => new Promise((resolve, reject) => {
    rejectRegistration = reject
    const timer = setTimeout(() => fail(new Error('provider registration timed out')), 30000)
    transport.once('providerReady', identity => { clearTimeout(timer); resolve(identity) })
    transport.once('failed', () => clearTimeout(timer))
    socket.write(JSON.stringify({ providerPid: pid }) + '\n')
  })
  transport.kill = () => {
    stopped = true
    lines.close()
    socket.end('{"done":true}\n')
  }
  return transport
}

function readProcessTable() {
  const table = execFileSync('/bin/ps', ['-axo', 'pid=,ppid=,pgid=,lstart='], {
    encoding: 'utf8', timeout: 2000, env: { ...process.env, LC_ALL: 'C', TZ: 'UTC' },
  })
  const entries = new Map()
  for (const line of table.trim().split('\n')) {
    const match = line.match(/^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*$/)
    if (!match) throw new Error('invalid host process snapshot')
    const entry = { pid: Number(match[1]), parent: Number(match[2]), group: Number(match[3]), started: match[4] }
    entries.set(entry.pid, entry)
  }
  return entries
}

export function captureProviderIdentity(pid, runnerPid) {
  const table = readProcessTable()
  const provider = table.get(pid)
  let ancestor = provider
  const seen = new Set()
  while (ancestor && !seen.has(ancestor.pid)) {
    if (ancestor.parent === runnerPid && table.has(runnerPid)) return provider
    seen.add(ancestor.pid)
    ancestor = table.get(ancestor.parent)
  }
  throw new Error('provider is not a descendant of its runner')
}

export function stopProviderTree(pid, expected) {
  const initial = readProcessTable().get(pid)
  if (!initial) return
  // A provider may legitimately change its job group after exec. Creation
  // identity survives that change; a recycled PID has a different start time.
  if (!expected || initial.pid !== expected.pid || initial.started !== expected.started) {
    throw new Error('provider identity changed; refusing to signal reused PID')
  }
  const frozen = new Map([[pid, initial]])
  const signal = (target, name) => {
    try { process.kill(target, name) } catch (error) {
      if (error.code !== 'ESRCH') throw error
    }
  }
  try {
    signal(pid, 'SIGSTOP')
    // Freeze before resnapshotting so descendants cannot race the final kill
    // by forking. Keep the original terminal session and /dev/tty intact.
    for (let pass = 0; pass < 10; pass++) {
      const children = new Map()
      for (const entry of readProcessTable().values()) {
        if (!children.has(entry.parent)) children.set(entry.parent, [])
        children.get(entry.parent).push(entry)
      }
      let added = false
      const pending = [...frozen.keys()]
      while (pending.length) {
        for (const descendant of children.get(pending.pop()) ?? []) {
          if (!frozen.has(descendant.pid)) {
            signal(descendant.pid, 'SIGSTOP')
            frozen.set(descendant.pid, descendant)
            pending.push(descendant.pid)
            added = true
          }
        }
      }
      if (!added) return
    }
    throw new Error('provider process tree did not quiesce')
  } finally {
    const current = readProcessTable()
    for (const [target, identity] of [...frozen].reverse()) {
      const latest = current.get(target)
      if (latest && latest.started === identity.started) {
        signal(target, 'SIGKILL')
      }
    }
  }
}

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
  let child
  let transport
  let monitorFailure
  let providerIdentity
  const stopChild = () => {
    if (!child?.pid) return
    if (providerIdentity) stopProviderTree(child.pid, providerIdentity)
    else child.stdio[3]?.end() // Closing the gate prevents an unregistered exec.
  }
  try {
    if (process.platform === 'darwin') {
      const socketPath = process.env.GOBBY_SRT_MONITOR_SOCKET
      if (!socketPath) throw new Error('missing shared SRT violation monitor')
      globalThis.gobbySrtLogTransport = suffix => {
        transport = sharedTransport(socketPath, suffix, error => {
          monitorFailure = error
          try { stopChild() } catch (cleanupError) {
            monitorFailure = new Error(`fail-closed provider cleanup failed: ${cleanupError.message}`, { cause: cleanupError })
          }
        })
        return transport
      }
    }
    await SandboxManager.initialize(parsed.data, undefined, true)
    if (process.platform === 'darwin') {
      if (!transport) throw new Error('SRT did not attach the shared violation monitor')
      await transport.ready
    }
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
    delete childEnv.GOBBY_SRT_MONITOR_SOCKET
    if (muxTmpdir) {
      if (providerTmpdir === undefined) delete childEnv.TMPDIR
      else childEnv.TMPDIR = providerTmpdir
      if (providerClaudeTmpdir === undefined) delete childEnv.CLAUDE_CODE_TMPDIR
      else childEnv.CLAUDE_CODE_TMPDIR = providerClaudeTmpdir
    }
    if (monitorFailure) throw monitorFailure
    const gated = process.platform === 'darwin'
    const gateScript = 'IFS= read -r gate <&3 && [ "$gate" = go ] && exec 3<&- && exec "$@"'
    child = spawn(gated ? '/bin/sh' : wrapped.argv[0], gated
      ? ['-c', gateScript, 'gobby-srt', ...wrapped.argv]
      : wrapped.argv.slice(1), {
      cwd: process.cwd(),
      env: childEnv,
      stdio: gated ? ['inherit', 'inherit', 'inherit', 'pipe'] : 'inherit',
    })
    const exited = new Promise(resolve => {
      child.once('error', error => resolve({ error }))
      child.once('exit', (code, signal) => resolve({ code: code ?? 1, signal }))
    })

    for (const signal of signals) {
      const handler = () => {
        if (child.exitCode === null && child.signalCode === null) {
          child.kill(signal)
        }
      }
      signalHandlers.set(signal, handler)
      process.on(signal, handler)
    }
    if (transport && child.pid) {
      providerIdentity = await transport.providerStarted(child.pid)
      child.stdio[3].end('go\n')
    }
    outcome = await exited
    if (outcome.error) throw outcome.error
    if (monitorFailure) throw monitorFailure
  } catch (error) {
    failure = error
  } finally {
    delete globalThis.gobbySrtLogTransport
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

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) try {
  if (process.argv[2] === '--monitor') {
    await monitor(process.argv[3], process.argv[4] === undefined ? undefined : Number(process.argv[4]))
  }
  else process.exitCode = await main()
} catch (error) {
  console.error(`gobby-srt: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
