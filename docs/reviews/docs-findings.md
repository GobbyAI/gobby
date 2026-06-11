# Review: docs accuracy — code bugs surfaced by guide verification

- **Scope:** `docs/guides/*.md`, first half alphabetical — `README.md` through
  `memory.md` (20 of 47 guides, 7,196 of 14,314 lines). Split boundary for the
  sibling task: guides B starts at `observability.md`. Entries here are cases
  where the doc states the clearly-intended contract and the CODE violates it;
  pure doc drift was fixed directly in the guides in the same commit.
- **Reviewer:** Claude Fable 5 (7 parallel verifier agents; every finding
  re-verified against source by the synthesizer)
- **Commit / branch:** `0.5.0` @ `07db2d036` (state reviewed)
- **Summary:** 0 Blocker · 5 Important · 2 Nit — the guides were largely
  honest; where doc and code disagree on intent, the code is the drifted side
  in these seven cases.

## Findings

### [IMPORTANT] gwiki timeout envelope omits contracted diagnostic fields

- **Where:** `src/gobby/gwiki_gateway.py:370-382` (`_timeout_envelope`),
  kill path at `src/gobby/gwiki_gateway.py:292-304`; contract at
  `docs/guides/gwiki-daemon-web.md:69-72`
- **Failure mode:** The guide's timeout contract says a timeout result "must
  identify the command, scope, timeout value, elapsed time when known,
  stderr/stdout captured before termination". The envelope carries only
  `{ok, command, status, payload: None, stderr: "", error: {type, message}}` —
  no scope, no timeout value (even though `self._timeout_seconds` is in hand),
  no elapsed time, and the kill path never reads the pipes, so stderr/stdout
  are always empty.
- **Why it matters:** Timeout diagnostics are the only signal an operator gets
  for hung gwiki commands; an empty envelope makes degradation guidance
  impossible. The doc contract was left as written (it is the intent).
- **Minimal fix:** Include `scope` identity and `error.timeout_seconds` in
  `_timeout_envelope`; capture whatever the pipes hold before kill.
- **Confidence:** high
- **Related:** the Gateway Error Handling table in the same guide previously
  promised `exit_code`/`stdout_json`/`raw_stdout`/`scope`/`degradations`
  fields that were never implemented anywhere (`gcode grep 'raw_stdout'
  src/gobby` → no matches). That table was rewritten to the implemented
  envelope shape; if the richer contract is still wanted, it should be a code
  task, not a doc revert.

### [IMPORTANT] `sse` transport accepted by validation but unconnectable

- **Where:** `src/gobby/mcp_proxy/models.py:119,145` and
  `src/gobby/config/mcp.py:219` accept `"sse"`;
  `src/gobby/mcp_proxy/transports/factory.py:32-41` has no SSE entry and
  raises `Unsupported transport: sse` at connection time
- **Failure mode:** An MCP server registered with `transport: "sse"` passes
  config/registry validation and is stored, then fails every connection
  attempt with a ValueError raised from the transport factory.
- **Why it matters:** Validation-time acceptance plus connect-time rejection is
  a stored-but-broken state the user can't diagnose from the add flow; the
  failure surfaces later and far from the cause.
- **Minimal fix:** Either remove `"sse"` from the accepted transport lists in
  `models.py`/`config/mcp.py`, or implement an SSE transport in
  `transports/`. Rejecting at validation is the smaller change.
- **Confidence:** high

### [IMPORTANT] cron CLI cannot address jobs by name despite storage support

- **Where:** `src/gobby/cli/cron.py:139-260` (run/toggle/runs/edit/remove all
  call `storage.get_job(job_id)`); `src/gobby/storage/cron.py:218-221`
  (`get_job` is UUID-only); `src/gobby/storage/cron.py:223`
  (`get_job_by_name` exists but has no CLI caller)
- **Failure mode:** Every cron CLI mutation/inspection command fails with
  "Job not found" when given a job name; only UUIDs resolve. The guide
  previously documented `NAME_OR_ID` (now corrected to `JOB_ID`).
- **Why it matters:** Names are the human handle (`cron add -n nightly-health`
  requires one); forcing a `cron list` → copy-UUID round-trip for every
  operation is needless friction, and the storage layer already supports the
  lookup.
- **Minimal fix:** In the cron CLI's job resolution, fall back to
  `get_job_by_name` when the argument is not a UUID.
- **Confidence:** high

### [IMPORTANT] comms secret category `"comms"` is not a valid category

- **Where:** `src/gobby/communications/manager.py:585` stores channel secrets
  with `category="comms"`; `src/gobby/storage/secrets.py:35` defines
  `VALID_CATEGORIES = {"general", "llm", "mcp_server", "memory", "integration"}`
- **Failure mode:** Channel secret writes carry a category the secrets store
  does not recognize as valid (the guide documents this mismatch as a known
  limitation — doc accurate, code inconsistent).
- **Why it matters:** Category-filtered secret listing/cleanup misses comms
  secrets; the validation set and the writers disagree about the taxonomy.
- **Minimal fix:** Add `"comms"` to `VALID_CATEGORIES` or switch the manager
  to `"integration"`.
- **Confidence:** high

### [IMPORTANT] `gobby comms send` posts to a route that does not exist

- **Where:** `src/gobby/cli/communications.py:102` posts to `/api/comms/send`;
  `src/gobby/servers/routes/communications.py:20-174` registers no send route
- **Failure mode:** The CLI send command 404s on every invocation. (The guide
  documents this as a known limitation; the CLI surface still ships the
  command.)
- **Why it matters:** A shipped CLI command that can never succeed.
- **Minimal fix:** Add the `POST /api/comms/send` route backed by the
  manager's outbound path, or remove/hide the CLI command until it exists.
- **Confidence:** high

### [NIT] `gobby comms status` sends a query param the route ignores

- **Where:** `src/gobby/cli/communications.py:56` requests `?status=true`;
  the channels route ignores the `status` param
  (`src/gobby/servers/routes/communications.py`)
- **Note:** Harmless today, but the CLI encodes an expectation the API never
  honors — drift that will bite when someone "fixes" either side alone.

### [NIT] Telegram polling webhook path disagrees with the comms webhook mount

- **Where:** `src/gobby/communications/adapters/telegram.py:83` builds
  `/v1/comms/webhooks/{config.id}` while inbound comms webhooks mount under
  `/api/comms/webhooks/...`
- **Note:** Only relevant when a `webhook_base_url` is configured for
  Telegram (polling is the default); the constructed registration URL would
  not match the served route. Documented in the guide as a limitation.

## Systemic patterns

Two recur. First, **aspirational contracts written as present tense**: the
gwiki gateway error/timeout tables and the comms guide's limitation notes both
describe richer behavior than the code ships; the guides drift into "must"
language for fields no writer produces. Docs that state contracts should be
backed by a test or marked as design intent. Second, **validation/consumer
splits**: `sse` passes validation but cannot connect; `"comms"` is written but
not valid; `?status=true` is sent but never read. In each case two layers
disagree about an enum or surface and nothing forces them to converge —
shared constants (transport map, category set) imported by both sides would
close the class.
