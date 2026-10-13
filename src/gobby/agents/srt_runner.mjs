#!/usr/bin/env node

import { appendFileSync, chmodSync, readFileSync, unlinkSync, writeSync, closeSync, openSync, fchmodSync, fstatSync, renameSync, constants } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { spawn, execFile } from 'node:child_process'
import { createConnection, createServer } from 'node:net'
import { EventEmitter } from 'node:events'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { resolve, dirname, join } from 'node:path'
import { promisify } from 'node:util'
import { open, unlink, rename, readdir, mkdir, link, lstat } from 'node:fs/promises'
import process from 'node:process'
import { getPriority, setPriority } from 'node:os'

import {
  SandboxManager,
  SandboxRuntimeConfigSchema,
} from './node_modules/@anthropic-ai/sandbox-runtime/dist/index.js'

// This host process outlives daemon restarts. Route complete eventMessage
// records by SRT's random session suffix, never by a provider PID.
const runnerIdentity = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex')
const execFileAsync = promisify(execFile)

const backlogSuffix = '.srt-backlog.jsonl'
const backlogLimit = 32 * 1024 * 1024
const validRunKey = key => typeof key === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(key)

// One private, run-keyed queue is also the failure snapshot. The unacked bound
// includes queued writes; retain the triggering record before failing closed.
class Backlog {
  constructor(directory, metadata) {
    this.path = join(directory, metadata.runKey + backlogSuffix)
    this.activePath = this.path + '.active'
    this.header = JSON.stringify({ monitorBacklog: metadata }) + '\n'
    this.headerBytes = Buffer.byteLength(this.header)
    this.file = null
    this.ownsFile = false
    this.length = 0
    this.bytes = 0
    this.entries = new Map()
    this.sendQueue = []
    this.cursor = 0
    this.tail = Promise.resolve()
  }
  serial(operation) {
    const result = this.tail.then(operation)
    this.tail = result.catch(() => {})
    return result
  }
  async create() {
    if (this.file) return
    const prior = await lstat(this.path).catch(error => { if (error.code !== 'ENOENT') throw error; return null })
    if (prior) throw new Error('unrecovered monitor backlog already exists')
    this.file = await open(this.activePath, constants.O_RDWR | constants.O_APPEND | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    this.ownsFile = true
    await this.file.writeFile(this.header)
    this.length = this.headerBytes
  }
  initialize() { return this.serial(() => this.create()) }
  add(record) {
    const data = Buffer.from(JSON.stringify(record) + '\n')
    this.bytes += data.length
    const full = this.bytes > backlogLimit
    const stored = this.serial(async () => {
      await this.create()
      const offset = this.length
      await this.file.writeFile(data)
      this.length += data.length
      this.entries.set(record.id, { offset, size: data.length })
      this.sendQueue.push(record.id)
    })
    return { full, stored }
  }
  next() {
    return this.serial(async () => {
      const id = this.sendQueue[this.cursor]
      if (id === undefined) return null
      this.cursor++
      if (this.cursor >= 1024) {
        this.sendQueue = this.sendQueue.slice(this.cursor)
        this.cursor = 0
      }
      const entry = this.entries.get(id)
      if (!entry || !this.file) throw new Error('invalid monitor backlog cursor')
      const data = Buffer.alloc(entry.size)
      let count = 0
      while (count < data.length) {
        const { bytesRead } = await this.file.read(data, count, data.length - count, entry.offset + count)
        if (!bytesRead) throw new Error('incomplete monitor backlog record')
        count += bytesRead
      }
      return data
    })
  }
  ack(id) {
    return this.serial(async () => {
      if (this.entries.keys().next().value !== id) throw new Error('invalid monitor acknowledgment order')
      this.bytes -= this.entries.get(id).size
      this.entries.delete(id)
      const first = this.entries.values().next().value
      if (!first) await this.discard()
      else if (first.offset - this.headerBytes >= 1024 * 1024) await this.compact()
    })
  }
  async discard() {
    await this.file?.close()
    this.file = null
    if (this.ownsFile) await unlink(this.activePath).catch(error => { if (error.code !== 'ENOENT') throw error })
    this.ownsFile = false
    this.length = 0
    this.entries.clear()
    this.sendQueue = []
    this.cursor = 0
  }
  async compact() {
    const first = this.entries.values().next().value
    if (!first) { await this.discard(); return }
    if (first.offset === this.headerBytes) return
    const temporaryPath = this.activePath + '.compact'
    const temporary = await open(temporaryPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    try {
      await temporary.writeFile(this.header)
      const buffer = Buffer.alloc(65536)
      let position = first.offset
      while (position < this.length) {
        const { bytesRead } = await this.file.read(buffer, 0, Math.min(buffer.length, this.length - position), position)
        if (!bytesRead) throw new Error('incomplete monitor backlog during compaction')
        await temporary.writeFile(buffer.subarray(0, bytesRead))
        position += bytesRead
      }
      await temporary.close()
      await rename(temporaryPath, this.activePath)
      await this.file.close()
      this.file = await open(this.activePath, constants.O_RDWR | constants.O_APPEND | constants.O_NOFOLLOW)
      const delta = first.offset - this.headerBytes
      for (const entry of this.entries.values()) entry.offset -= delta
      this.length -= delta
    } finally {
      await temporary.close().catch(() => {})
      await unlink(temporaryPath).catch(error => { if (error.code !== 'ENOENT') throw error })
    }
  }
  finish(done) {
    return this.serial(async () => {
      if (done) { await this.discard(); return }
      await this.compact()
      await this.file?.close()
      this.file = null
      if (this.ownsFile) {
        // Publish only a completed snapshot; never overwrite an earlier failure.
        await link(this.activePath, this.path)
        await unlink(this.activePath)
        this.ownsFile = false
      }
    })
  }
}

async function recoverBacklogs(directory, retentionDirectory, requestedKey) {
  if (requestedKey !== undefined && !validRunKey(requestedKey)) throw new Error('invalid recovery run key')
  const pattern = /^([a-zA-Z0-9_-]{1,128})\.srt-backlog\.jsonl(?:\.active)?$/
  const names = (await readdir(directory)).filter(name => pattern.test(name) && (requestedKey === undefined || name.match(pattern)[1] === requestedKey))
  if (!names.length) return false
  const processes = await readProcessTable()
  let blocked = false
  for (const name of names) {
    const path = join(directory, name)
    let source
    try {
      source = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
      const info = await source.stat()
      if (!info.isFile() || info.uid !== process.getuid()) throw new Error('unsafe monitor backlog')
      const buffer = Buffer.alloc(16384)
      const { bytesRead } = await source.read(buffer, 0, buffer.length, 0)
      const header = JSON.parse(buffer.subarray(0, bytesRead).toString().split('\n')[0]).monitorBacklog
      const runKey = name.match(pattern)[1]
      if (header?.runKey !== runKey) throw new Error('monitor backlog run key mismatch')
      if (name.endsWith('.active') && header.brokerStarted && processes.get(header.brokerPid)?.started === header.brokerStarted) {
        blocked = true
        continue
      }
      if (requestedKey === undefined && header.runnerStarted && processes.get(header.runnerPid)?.started === header.runnerStarted) continue
      await mkdir(retentionDirectory, { recursive: true, mode: 0o700 })
      const destination = join(retentionDirectory, runKey + '.monitor-backlog.jsonl')
      const retained = await open(destination, constants.O_APPEND | constants.O_CREAT | constants.O_WRONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK, 0o600)
      try {
        const retainedInfo = await retained.stat()
        if (!retainedInfo.isFile() || retainedInfo.uid !== process.getuid()) throw new Error('unsafe monitor backlog retention')
        await retained.chmod(0o600)
        let position = 0
        while (true) {
          const { bytesRead: count } = await source.read(buffer, 0, buffer.length, position)
          if (!count) break
          await retained.writeFile(buffer.subarray(0, count))
          position += count
        }
      } finally { await retained.close() }
      await unlink(path)
      await unlink(path + '.compact').catch(error => { if (error.code !== 'ENOENT') throw error })
      console.error(`gobby-srt monitor: recovered backlog for ${runKey} at ${destination}`)
    } catch (error) {
      if (error.code === 'ENOENT') continue
      // Preserve raw evidence outside both recovery and subscription patterns.
      // A torn header, unsafe entry or retention failure must not wedge launch.
      const quarantine = path + '.quarantine-' + randomUUID()
      let disposition = `quarantined ${name} at ${quarantine}`
      try {
        await rename(path, quarantine)
        await rename(path + '.compact', quarantine + '.compact').catch(failure => { if (failure.code !== 'ENOENT') throw failure })
      } catch (failure) {
        disposition = `could not quarantine ${name}; raw evidence preserved: ${failure.message}`
      }
      console.error(`gobby-srt monitor: ${disposition}: ${error.message}`)
    } finally { await source?.close() }
  }
  return blocked
}

async function monitor(socketPath, readyFd, retentionDirectory) {
  setPriority(0, 19)
  const clients = new Map()
  const cleanups = new Set()
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
  const finishConsumer = async state => {
    if (state.finished) return
    state.finished = true
    await Promise.all([
      state.backlog?.finish(state.done).catch(error => console.error(`gobby-srt monitor backlog retention failed for ${state.runKey}: ${error.message}`)),
      !state.done && state.providerPid !== null
        ? stopProviderTree(state.providerPid, state.providerIdentity).catch(error => console.error(`gobby-srt monitor provider cleanup failed: ${error.message}`))
        : undefined,
    ])
  }
  const pump = async (socket, state) => {
    if (state.pumping || state.blocked || state.finished) return
    state.pumping = true
    try {
      while (!state.blocked && !state.finished && !socket.destroyed) {
        const record = await state.backlog.next()
        if (!record || state.finished) break
        state.blocked = !socket.write(record)
      }
    } catch (error) {
      console.error(`gobby-srt monitor delivery failed for ${state.runKey}: ${error.message}`)
      socket.destroy()
    } finally { state.pumping = false }
  }
  const armIdle = () => {
    clearTimeout(idleTimer)
    if (clients.size === 0 && !stopping) idleTimer = setTimeout(stop, 30000)
  }
  const acknowledge = socket => socket.write(JSON.stringify({ ready: true, identity: runnerIdentity }) + '\n')
  const markReady = () => {
    if (streamReady) return
    streamReady = true
    if (readyFd !== undefined) { writeSync(readyFd, 'ready\n'); closeSync(readyFd) }
    for (const [socket, state] of clients) if (state.registered) acknowledge(socket)
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
    const state = { suffix: null, registered: false, runKey: null, runnerPid: null, providerPid: null, providerIdentity: null, done: false, finished: false, backlog: null, blocked: false, pumping: false, queue: Promise.resolve() }
    clients.set(socket, state)
    socket.setTimeout(5000, () => socket.destroy())
    const lines = createInterface({ input: socket, crlfDelay: Infinity })
    lines.on('line', line => {
      state.queue = state.queue.then(async () => {
        const request = JSON.parse(line)
        if (state.done || state.finished) throw new Error('closed monitor subscription')
        if (state.suffix === null && typeof request.suffix === 'string' && /^_[a-z0-9]+_SBX$/.test(request.suffix)) {
          state.suffix = request.suffix
          if (request.suffix !== '_gobbyprobe_SBX') {
            if (!validRunKey(request.runKey)) throw new Error('invalid monitor run key')
            for (const [otherSocket, otherState] of clients) {
              if (otherSocket !== socket && otherState.runKey === request.runKey) throw new Error('monitor run key already subscribed')
            }
            state.runKey = request.runKey
          }
          if (request.runnerPid !== undefined) {
            if (!Number.isSafeInteger(request.runnerPid) || request.runnerPid <= 1) throw new Error('invalid runner PID')
            state.runnerPid = request.runnerPid
          }
          const processes = state.runnerPid === null ? new Map() : await readProcessTable()
          const runnerStarted = processes.get(state.runnerPid)?.started
          if (state.runnerPid !== null && !runnerStarted) throw new Error('runner identity unavailable')
          if (state.runKey !== null) {
            state.backlog = new Backlog(dirname(socketPath), { runKey: state.runKey, runnerPid: state.runnerPid, runnerStarted, brokerPid: process.pid, brokerStarted: processes.get(process.pid)?.started, suffix: state.suffix })
            await state.backlog.initialize()
            if (state.finished || socket.destroyed) await state.backlog.finish(false)
          }
          socket.setTimeout(0)
          state.registered = true
          if (streamReady) acknowledge(socket)
        } else if (Number.isSafeInteger(request.ack) && state.backlog) {
          await state.backlog.ack(request.ack)
        } else if (Number.isSafeInteger(request.providerPid) && request.providerPid > 1 && state.providerPid === null) {
          if (state.runnerPid === null) throw new Error('provider registration needs its runner')
          const identity = await captureProviderIdentity(request.providerPid, state.runnerPid)
          if (state.finished || socket.destroyed) {
            await stopProviderTree(request.providerPid, identity)
            throw new Error('monitor closed during provider registration')
          }
          state.providerIdentity = identity
          state.providerPid = request.providerPid
          socket.write(JSON.stringify({ providerReady: true, identity: state.providerIdentity }) + '\n')
        } else if (request.done === true) {
          state.done = true
        } else throw new Error('invalid monitor request')
      })
      state.queue.catch(() => socket.destroy())
    })
    socket.on('error', () => socket.destroy())
    socket.on('drain', () => { state.blocked = false; void pump(socket, state) })
    socket.on('close', () => {
      clients.delete(socket)
      lines.close()
      // This also stops a provider whose runner was killed or SIGSTOPped and
      // therefore cannot process its own disconnected-transport callback.
      const cleanup = state.queue.catch(() => {}).then(() => finishConsumer(state))
      cleanups.add(cleanup)
      cleanup.finally(() => cleanups.delete(cleanup))
      armIdle()
    })
  })
  const serverClosed = new Promise(resolve => server.once('close', resolve))
  try {
    await recoverBacklogs(dirname(socketPath), retentionDirectory)
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
        let matched = false
        for (const [socket, state] of clients) {
          if (!socket.destroyed && !state.finished && state.backlog && state.suffix === suffix) {
            matched = true
            const { full, stored } = state.backlog.add({ id, message })
            stored.then(() => pump(socket, state)).catch(error => {
              console.error(`gobby-srt monitor backlog write failed for ${state.runKey}: ${error.message}`)
              socket.destroy()
            })
            if (full) {
              console.error(`gobby-srt monitor: ${state.runKey} exceeded 32 MiB backlog; closing its subscription`)
              const cleanup = finishConsumer(state)
              cleanups.add(cleanup)
              cleanup.finally(() => cleanups.delete(cleanup))
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
  } finally {
    stop()
    if (listening) await serverClosed
    await Promise.allSettled([...cleanups])
  }
}

function sharedTransport(socketPath, suffix, failed, runKey = process.env.GOBBY_SRT_RUN_KEY) {
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
  socket.once('connect', () => socket.write(JSON.stringify({ suffix, runnerPid: process.pid, runKey }) + '\n'))
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

async function readProcessTable() {
  const { stdout: table } = await execFileAsync('/bin/ps', ['-axo', 'pid=,ppid=,pgid=,lstart='], {
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

export async function captureProviderIdentity(pid, runnerPid) {
  const table = await readProcessTable()
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

export async function stopProviderTree(pid, expected) {
  // This is attached-descendant cleanup, not a lifetime containment API.
  // Already-reparented double-fork/setsid workers survive inside their inherited
  // macOS sandbox after the shared monitor closes; they are then unmonitored.
  const initial = (await readProcessTable()).get(pid)
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
      for (const entry of (await readProcessTable()).values()) {
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
    const current = await readProcessTable()
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
  let gateError
  let cleanup = Promise.resolve()
  const stopChild = async () => {
    if (!child?.pid) return
    if (providerIdentity) await stopProviderTree(child.pid, providerIdentity)
    else child.stdio[3]?.end() // Closing the gate prevents an unregistered exec.
  }
  try {
    if (process.platform === 'darwin') {
      const socketPath = process.env.GOBBY_SRT_MONITOR_SOCKET
      if (!socketPath) throw new Error('missing shared SRT violation monitor')
      globalThis.gobbySrtLogTransport = suffix => {
        transport = sharedTransport(socketPath, suffix, error => {
          monitorFailure = error
          cleanup = stopChild().catch(cleanupError => {
            monitorFailure = new Error(`fail-closed provider cleanup failed: ${cleanupError.message}`, { cause: cleanupError })
          })
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
    delete childEnv.GOBBY_SRT_RUN_KEY
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
      child.stdio[3]?.once('error', error => {
        gateError = error
        resolve({ error })
      })
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
      if (gateError) throw gateError
      await new Promise((resolve, reject) => {
        child.stdio[3].once('error', reject)
        child.stdio[3].end('go\n', error => error ? reject(error) : resolve())
      })
      if (gateError) throw gateError
    }
    outcome = await exited
    if (outcome.error) throw outcome.error
    if (monitorFailure) throw monitorFailure
  } catch (error) {
    failure = error
    try { await stopChild() } catch (cleanupError) {
      failure = new Error(`provider cleanup failed: ${cleanupError.message}`, { cause: error })
    }
  } finally {
    await cleanup
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
  if (process.argv[2] === '--recover') {
    setPriority(0, 19)
    process.exitCode = await recoverBacklogs(process.argv[3], process.argv[4], process.argv[5]) ? 2 : 0
  } else if (process.argv[2] === '--monitor') {
    await monitor(process.argv[3], process.argv[4] === undefined ? undefined : Number(process.argv[4]), process.argv[5])
  }
  else process.exitCode = await main()
} catch (error) {
  console.error(`gobby-srt: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
