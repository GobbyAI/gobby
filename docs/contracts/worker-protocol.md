# Worker Protocol Contract

Status: protocol version 1, R0 (gobby#25948). The frozen corpus is
`tests/contracts/worker_protocol.json`. The Python models are in
`src/gobby/worker/protocol.py`, and the guard order is in `src/gobby/worker/facade.py`.
The Rust client is `crates/gdaemon/src/lifecycle/worker_handshake.rs` (dormant under the Python
default; the Rust handshake runs when RUNNER_DEFAULT is Rust).

This contract is private to the daemon and its worker. It is not a public API.

## Transport and encoding

- Every request and every response is UTF-8 JSON bytes, carried in the length-prefixed frames
  described in Transport and framing.
- A request is stored and replayed exactly as a client sends it. Requests are not canonical.
- A response is canonical: `json.dumps(value, separators=(",", ":"), sort_keys=True,
  ensure_ascii=False, allow_nan=False)` encoded as UTF-8.
- Request bytes are read with plain `json.loads`. Duplicate keys and `NaN` are not refused.
  Bytes that are not UTF-8 JSON, or whose top level is not an object, are `invalid_request`.

## Transport and framing

- The channel is one connected Unix socket pair, created by gdaemon for each spawn in Rust mode.
  gdaemon keeps the parent end, which is close-on-exec. The child inherits only the child end. There
  is no listener, socket path or port, so the connection is bound to that one child and is private
  to this machine. The supervisor that spawns workers is built for Unix only, and a marked start
  refuses on Windows.
- Three variables mark a start that serves the channel. Only gdaemon sets them, and only in Rust
  mode, in the same step that creates the descriptors, so a child never sees the marker without
  them:
  - `GOBBY_WORKER_MARKER` is `1`.
  - `GOBBY_WORKER_CHANNEL_FD` names the child end of the connection.
  - `GOBBY_WORKER_CREDENTIAL_FD` names the read end of the credential pipe.
  The Python default spawn sets none of them. Its argv, environment and inherited descriptors are
  unchanged.
- The credential is drawn per spawn: 32 random bytes, encoded as URL-safe base64 without padding
  (43 characters). gdaemon writes it into a pipe and closes the write end before the spawn. The
  read end is inherited and named by `GOBBY_WORKER_CREDENTIAL_FD`. The credential is never in argv
  or in any environment value.
- Python's `take_worker_boot` consumes all three variables before the inherited descriptors are
  sealed:
  - With no marker, the start is an ordinary start and takes no boot.
  - A marker other than `1` refuses the start. So does a marker on Windows.
  - A channel or credential variable that is missing, is not an integer, or does not name an
    inherited descriptor above stderr refuses the start, and the message names the variable.
  - A channel descriptor that is closed, or is open but is not a socket, refuses the start before
    the channel is used, and the message names the channel variable.
  - Otherwise the credential is read to end of stream and its descriptor is closed. The channel
    descriptor stays open for the worker.
  A refusal exits with status 78, and the supervisor treats that exit as sticky.
- A marked start serves the worker facade over the channel and does not construct the full runner.
  The worker-only boot registers no lifecycle phase and no domain op.
- Every frame is a 4-byte unsigned big-endian length followed by that many payload bytes. The
  payload is one request or response envelope. Each request frame gets exactly one reply frame, in
  order.
- A reader checks the length prefix against its limit before it reads or allocates the body.
  - The Python worker refuses a request frame whose prefix is over 16 MiB (16,777,216 bytes) and
    closes the channel. It also refuses to send a reply over 16 MiB and closes the channel. The
    request bounds in Bounds apply to each request once it is read.
  - The Rust client refuses a reply frame whose prefix is over the limit for its command, and
    reports a channel error.
- The Rust client's reply limits are 65,536 bytes for `handshake` and `initialize`, and 16 MiB
  (16,777,216 bytes) for `invoke`. The Python worker sends no reply over its 16 MiB frame limit.
- A stream that ends before a whole prefix or body arrives is end of stream. The Python worker
  closes the channel. The Rust client reports a channel error.
- The Rust client waits at most 60 seconds for a `handshake` reply and at most 300 seconds for an
  `initialize` or `invoke` reply. A reply that misses its deadline is a channel error.
- Each call makes one exchange. A channel error, an end of stream, a framing error, a reply over
  its limit or a missed deadline is an unknown outcome for that call (see Unknown outcome). The
  client never resends.

## Request envelope

A request is a JSON object with these keys and no others:

| Key | Type | Rule |
|---|---|---|
| `protocol_version` | int | from 1 to 2^64 - 1 (18,446,744,073,709,551,615). A value outside that range is `invalid_request`. A value in range that is not an accepted version is `protocol_mismatch`. |
| `generation` | int | from 1 to 2^64 - 1 (18,446,744,073,709,551,615). Zero or a value outside that range is `invalid_request`. |
| `operation_id` | str | 1 to 128 characters. Convention `"<generation>:<seq>"`, `seq` from 1. |
| `command` | str | `handshake`, `initialize` or `invoke`. Any other string is `unknown_command` once a handshake is bound. Before a handshake it is `not_handshaken` (step 7). |
| `body` | object | default `{}`. Its shape is checked per command. |

Integers are strict: a bool is refused where an int is expected, and a string
`"7"` is refused. Integers are in the wire range `[-2^63, 2^64 - 1]` only (see Bounds). A missing required key or an unknown key is `invalid_request`.

## Response envelope

A response is a JSON object with `protocol_version` (always 1 in R0), `generation`
(int or null), `operation_id` (str or null), and exactly one of:

- `result`: any encoded JSON value. A result of `0`, `false`, `null` or `{}` is a valid result.
- `error`: `{"code": <ErrorCode>, "message": <str>}`. The message is at most 2,048 characters.

Echo rule. `generation` and `operation_id` are echoed only from a parsed request whose own
value passes its own envelope rule (an integer outside the wire range does not). An unknown key does not prevent the echo. An
`operation_id` that cannot be written as UTF-8 (a lone surrogate from a JSON escape) fails
that rule, so it is not echoed and the request is `invalid_request`. A request that is not
parsed (over 16 MiB, or not JSON) is answered with both fields null.

Example, a handshake accepted:

```
{"generation":7,"operation_id":"7:1","protocol_version":1,"result":{"commands":["handshake","initialize","invoke"],"generation":7,"protocol_version":1,"schema_identity":{...}}}
```

## Versioning

- `WORKER_PROTOCOL_VERSION = 1`. `ACCEPTED_PROTOCOL_VERSIONS = (1,)`.
- Changes are additive. A new version adds a corpus block and an accepted entry. Older
  blocks stay in the corpus and are replayed. A version is removed only at S3-DELETE.
- At R0 landing no pinned or retained gdaemon speaks a worker protocol, so the accepted set
  is `{1}`.
- Corpus replay checks that the corpus `versions` list equals the accepted set.

## Generation

- The generation is assigned by the Rust side (`GenerationSource`, starting at 1 and never
  yielding 0). The first handshake binds it.
- A repeated handshake with the bound generation returns the same result and changes no state.
- A handshake or any other command with a different generation is `stale_generation`.
- Generations are sequential integers that start at 1. They are not secret. The
  credential admits a handshake: the handshake checks auth before it compares generations,
  and a refused handshake binds nothing.
- One worker process serves one generation. In Rust mode each spawn draws its generation once,
  and the child serves that generation.

## Credential and handshake

- The handshake body is exactly `{"credential": <non-empty string>}`. Any other shape,
  including an empty string, is `invalid_request`.
- The facade is built with a credential, or with `None`. With `None`, every handshake whose
  body is well formed is refused with `unauthorized`. A malformed body is refused first as
  `invalid_request`, because guard step 6 checks the body before the credential. There is no
  "allow when unconfigured" path.
- The body credential is compared with `hmac.compare_digest`. A mismatch is `unauthorized`
  with the message `credential refused`.
- The credential is never echoed, logged or stored after the comparison. Error text is constant.
  The worker receives the credential only through its own inherited pipe (see Transport and
  framing), never through argv or the environment.
- The handshake result carries `protocol_version`, `generation`, `commands` (sorted:
  `handshake`, `initialize`, `invoke`) and `schema_identity`, which is the release-pinned
  identity `expected_schema_identity()`. The Rust client compares it with the installed
  identity after the commands check. A mismatch is a protocol violation on the `schema_identity`
  field, and it binds no generation.
- Only the handshake carries the credential. `initialize` and `invoke` carry none. After a
  handshake binds, later commands are admitted by the connection itself: the channel is a
  socket pair held by one child, with no listener, path or port (see Transport and framing). No
  per-command credential is sent.
- A handshake whose schema identity cannot be loaded is `op_failed` with the message
  `schema identity unavailable`. It binds no generation, so the next command is
  `not_handshaken`.

## Commands and guard order

The facade applies these steps in order. The first failing step decides the response.

| Step | Check | Failure |
|---|---|---|
| 1 | Request is over 16 MiB (`DOMAIN_OP_MAX_REQUEST_BYTES`). Nothing is parsed. | `invalid_request`, echo null |
| 2 | UTF-8 JSON with an object at the top level | `invalid_request` |
| 3 | Command is `handshake` or `initialize` and the request is over 65,536 bytes (`MAX_REQUEST_BYTES`) | `invalid_request` |
| 4 | Envelope (table above) | `invalid_request` |
| 5 | `protocol_version` is not accepted | `protocol_mismatch` |
| 6 | `handshake`: body shape, then credential, then generation, then the result. The generation binds only after the result is built. | `invalid_request`, `unauthorized`, `stale_generation`, `op_failed` |
| 7 | No handshake bound (any other command, including unknown ones) | `not_handshaken` |
| 8 | Generation differs from the bound generation | `stale_generation` |
| 9 | Command is not `initialize` or `invoke` | `unknown_command` |
| 10 | `initialize`: body is exactly `{"phase": <known phase>}`; then unusable; then phase order | `invalid_request`, `worker_unusable`, `phase_out_of_order` |
| 11 | `invoke`: body is exactly `{"op", "params"}`; then unusable; then not ready; then op lookup; then params; then the op runs, and an op that raises an `Exception` is `op_failed` | `invalid_request`, `worker_unusable`, `not_ready`, `unknown_command`, `op_failed` |

`command` is a plain string at the envelope level, so an unknown command is `unknown_command`,
never `invalid_request`.

## Phases and statuses

Phases run in declaration order. Each phase is one of the five steps of `GobbyRunner.create`.

1. `storage_and_config`
2. `content_refusal`
3. `startup_content`
4. `config_runtime` (includes `configure_build_root`)
5. `runtime_services`

An `initialize` result has these keys, always present: `phase` (the requested phase),
`status`, `next_phase`, `message` and `error_type`.

| Status | Meaning | `next_phase` | `message` | `error_type` |
|---|---|---|---|---|
| `ready` | the phase completed | the following phase, or null on the last phase | null | null |
| `refused` | the phase raised `StartRefusal`; its rollback succeeded | null | `str(exc)`, at most 2,048 characters | null |
| `failed` | the phase raised another `Exception`, its rollback failed, or `create` parked out of order | null | `str(exc)`, `rollback failed: ...`, or `create entered a phase out of order`, at most 2,048 characters | the exception class name, `IncompleteCreate` when `create` returned before every phase ran, or `PhaseOrderViolation` when `create` parked out of order |

A refused or failed phase does not advance the next phase. The facade becomes unusable after
a refused or failed phase. A refused or failed phase reports `next_phase` as null.

A `create` that returns before every phase scope has run makes the phase being settled
`failed`, with `next_phase` null, the message `create returned before every phase ran`, and
the error type `IncompleteCreate`. No exception exists for that case, so the error type is
this fixed name. The facade is unusable from then on, so every later `initialize` and
`invoke` is `worker_unusable`.

A `create` that parks at a phase scope that is neither the requested phase nor the following
phase makes the phase being settled `failed`, with `next_phase` null, the message
`create entered a phase out of order`, and the error type `PhaseOrderViolation`. The facade
cancels the create while it waits at that scope and waits for its rollback to finish before it
answers. The facade is unusable from then on, so `initialize` never waits on a phase that cannot
come.

Construction: `WorkerFacade` builds a phase driver whose gate creates futures on the
running event loop. Construct the facade inside a running loop, from async code, never from
synchronous setup. The constructor takes `credential`, `ops` (default: the op table),
`create` (a zero-argument factory for the runner's create coroutine) and, only when `create`
is omitted, `config_path` and `verbose` for the real `GobbyRunner.create`.

Not-ready gating: an `invoke` is refused with `not_ready` until the handshake has bound
and all five phases have reported `ready`. After a refused, failed, cancelled or early-returning phase the
facade is unusable, and every later `initialize` and `invoke` is `worker_unusable`.

## Bounds

| Bound | Value | Applies to | Refusal |
|---|---|---|---|
| `MAX_REQUEST_BYTES` | 65,536 bytes | `handshake` and `initialize` requests | `invalid_request` after the command is read; 65,536 bytes is accepted, 65,537 is refused |
| `DOMAIN_OP_MAX_REQUEST_BYTES` | 16 MiB (16,777,216 bytes) | every request; invoke carries the tool arguments | `invalid_request` before any parse; 16 MiB is accepted, one byte more is refused |

Integers on the wire are in `[-2^63, 2^64 - 1]` only. A result that holds an integer
outside that range is `op_failed`. A request whose `generation` or `protocol_version` is outside
it is `invalid_request`, and its echo leaves out the generation.
An `Int` parameter outside it is `invalid_request`.
`Anything` and `JsonObject` values pass through as decoded JSON.
An integer nested in one reaches the op as it was decoded.
The Rust client reads envelope integers as `u64` or `i64`. It applies the same range to every
integer in a reply, before it parses the reply. A reply that holds an out-of-range integer is
malformed: a protocol violation for `handshake`, and an unknown outcome for `initialize` and
`invoke`.

Reply limits and reply deadlines are in Transport and framing.

Python and Rust keep separate constants. The Rust constant `MAX_DOMAIN_OP_REQUEST_BYTES`
is not shared with the hook ingress bound, which is private to its module.

## Error codes

| Code | Message (fixed unless noted) | When |
|---|---|---|
| `invalid_request` | `invalid request` | a request fails the envelope, a bound, or a body shape |
| `protocol_mismatch` | `protocol version not accepted` | `protocol_version` is not accepted |
| `unauthorized` | `credential refused` | no credential configured, or a wrong credential |
| `not_handshaken` | `no handshake for this worker` | a command other than handshake before any bind |
| `stale_generation` | `generation does not match the bound generation` | a generation other than the bound one |
| `unknown_command` | `unknown command` | an unknown command, or an unknown op name |
| `phase_out_of_order` | `phase is not the next phase` | an `initialize` for a phase that is not next, or a repeat |
| `worker_unusable` | `worker is unusable` | a refused, failed or cancelled phase has made the facade unusable |
| `not_ready` | `worker is not ready` | an `invoke` before the handshake and every phase are ready |
| `op_failed` | dynamic, at most 2,048 characters, or `schema identity unavailable` | an op raised an `Exception`, returned an unencodable value (including an integer outside the wire range), a registration fault occurred, or the handshake could not load its schema identity |

### Error codes per command

A command's error reply carries one of the codes in its row. Each row is the union of that
command's guard steps above, and the Rust client's code sets (`HANDSHAKE_ERROR_CODES`,
`INITIALIZE_ERROR_CODES` and `INVOKE_ERROR_CODES`) equal the rows.

| Command | Error codes |
|---|---|
| `handshake` | `invalid_request`, `protocol_mismatch`, `unauthorized`, `stale_generation`, `op_failed` |
| `initialize` | `invalid_request`, `protocol_mismatch`, `not_handshaken`, `stale_generation`, `worker_unusable`, `phase_out_of_order` |
| `invoke` | `invalid_request`, `protocol_mismatch`, `not_handshaken`, `stale_generation`, `worker_unusable`, `not_ready`, `unknown_command`, `op_failed` |

Out-of-set rule. A reply whose error code is outside its command's row is a protocol violation
for `handshake`. For `initialize` and `invoke` it is an unknown outcome, because the effect may
already have happened. `initialize` never answers `op_failed`: a failed phase is a `failed`
result, not an error.

Registration faults map to `op_failed`, never to `invalid_request`. They are the
`OpRegistrationError`, `TypeError`, `ImportError` and `NameError` raised by a broken
registration. An op module or dataclass module whose import raises any `Exception` is a
registration fault of the same kind. A dataclass whose annotations cannot be evaluated,
for any `Exception`, is a `TypeError` registration fault. The fault is a server fault,
and it fails only the call that hit it.

## Op table and invoke

- `invoke` takes the body `{"op": <str>, "params": {...}}`. `params` defaults to `{}` and must
  have exactly the keys the op declares; any other key set is `invalid_request`.
- The op table `OPS` is built from one `DomainOp(...)` line per op. Names are unique. R0
  registers no op.
- Resolution is lazy: the `"module:qualname"` path is resolved on first invoke, so importing
  `ops.py` loads no module an op names.
- Each parameter has a decoder: `Str`, `Int`, `Bool`, `Optional`, `IsoDatetime`,
  `Literal`, `JsonObject`, `Anything`, `DataclassFields`. A value the decoder refuses is
  `invalid_request`. An `Int` outside the wire range is refused. A dataclass whose
  constructor or `__post_init__` raises any `Exception` is `invalid_request` too.
- Sync and async functions are both allowed. A sync op runs inline on the worker loop and
  blocks every other invoke until it returns. An async op holds up no other invoke, and its
  futures resolve on the worker loop.
- An op that raises an `Exception` is `op_failed` with `str(exc)`. An op that raises a
  `BaseException` propagates with no response.
- Result encoding (`encode_value`): `None`, bool, str and finite floats as is; an int in
  `[-2^63, 2^64 - 1]` as is; an Enum
  as its value; a dataclass through `dataclasses.asdict` with `_type` set to its class name
  at the top level only; a datetime as its ISO string; a path as `str(path)` in the host's form, with backslashes separating on Windows and forward
slashes on POSIX; a set or
  frozenset as a list sorted by canonical text; a tuple or list as an array; an exception as
  `_type` plus its attributes (`vars`), or `_type` alone when it has no `__dict__`.
  Anything else (bytes, arbitrary objects, functions, modules, non-finite floats, ints outside the wire range, dicts with
  non-str keys, a dataclass field or dict key named `_type`) is `op_failed`. So is a value
  whose canonical text cannot be written as UTF-8 (a lone surrogate) or that nests past the
  recursion limit. The facade probes the canonical writer before it answers.
- A worker op binds no caller and no daemon-owned execution. The caller-ownership facts
  below apply to every op.

## Caller-ownership facts

- `require_machine_owned` returns at once for a worker op, because no caller scope is bound
  (`src/gobby/hub/caller_scope.py`).
- `is_daemon_execution()` reads `False` for a worker op, because no daemon-execution scope is
  set (`src/gobby/utils/session_context.py`).
- A native route that exposes a worker op must check machine ownership itself before it
  calls the op. The `daemon_execution` recipient exemption cannot fire from a worker op. These
  facts are frozen in the `ops.py` module docstring and are tested.

## Unknown outcome

- The Rust client makes exactly one exchange per call.
- A lost reply (a `ChannelError` on the exchange) is returned as a distinct unknown outcome for
  all three commands. It is never an error that invites retry, and the client never resends.
- For `initialize` and `invoke`, a reply that fails validation is also an unknown outcome,
  because the effect may already have happened. A handshake reply that fails validation is a
  protocol violation.
- A reply whose error code is outside its command's row in the per-command table is a protocol
  violation for `handshake`, and an unknown outcome for `initialize` and `invoke`.
- An unknown outcome on `handshake` or `initialize` ends the worker child. The next spawn takes a
  new generation, and the ended generation is refused as stale. R1 registers no lifecycle phase,
  so R2 inherits this rule for the phases it registers.
- Reconciliation belongs to the caller. Reconciling lifecycle phases belongs to R2. The
  caller chunk reconciles domain ops. An `op_failed` reply means the op ran and may have had a
  partial effect.

## runner_protocol

`runner_protocol` is an integer field of the gdaemon schema identity
(`src/gobby/storage/schema_identity_pin.py`). It appears inside `schema_identity` in the
handshake result. It is a separate counter from `protocol_version`, which versions this wire
format. A change to one never implies a change to the other.

## Corpus

`tests/contracts/worker_protocol.json` is frozen, with exact request and response bytes.
It has one version block (`protocol_version` 1) with 17 request cases and 5 response vectors.

- `credential` is `corpus-credential`.
- The handshake result's `schema_identity` is stored as the placeholder string
  `"$schema_identity"`. Replay substitutes that exact string with the canonical text of
  `expected_schema_identity()`. A corpus with a literal identity object fails the replay.
- The oversize cases (over 64 KiB and over 16 MiB) are built by the tests, not stored.
- The Rust tests read the same corpus through `CARGO_MANIFEST_DIR`.

## R1 hand-off

R1 wires the live boot. Each item lives in the section named after it:

- the transport and framing for the three commands: Transport and framing;
- the generation of each spawn: Generation;
- comparing the returned `schema_identity` with the installed identity: Credential and handshake;
- the credential, which is per spawn and rides its own inherited pipe: Transport and framing;
- the connection, bound to one child and private to the machine, which stands in for a per-command
  credential: Credential and handshake;
- unknown outcomes for lifecycle phases: Unknown outcome, where R2 inherits the rule;
- the runner-mode switch that selects the Rust handshake: the status line at the top of this file.

R1 registers no domain op and no lifecycle phase.
