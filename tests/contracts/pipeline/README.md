# Hook pipeline corpus

This directory holds the committed contract for one hook delivery: the route's adapter step
(`handle_native`) into `HookManager.handle`, the event handler, and every collaborator the
pipeline reads.

- `manifest.json` names the schema version, the case kinds, the collaborator registry, the
  collaborators that are injected but never read (`unread_collaborators`), the required
  scenarios, the sources and the platform record.
- `cases/handlers.json`: one `event` case per handler-map event type, delivered from a native
  provider payload. An event type that no adapter the hooks route builds maps a hook to
  (`before_tool_selection`, listed only by the abstract ACP base adapter) is delivered as the
  hook event itself and says why in `reason`; a test fails if a routed adapter maps it.
- `cases/session_start.json`: session start for resumed, cleared and compacted sessions.
- `cases/edge.json`: a first prompt that activates a deferred session, a node delivery whose
  session has no row, a blocking webhook, a synthetic rule block that short-circuits the
  handler, and a resume whose response carries the session banner.
- `cases/failures.json`: one `collaborator_failure` case per collaborator kind. The failure
  lives only in `failing_collaborator` and `failure`; removing those keys leaves the healthy
  twin, whose outcome differs. A collaborator that is injected but never read (`call_tool`)
  has a failing case too: it records the healthy response, and the reach check makes the
  case's handler read the collaborator, which must change the outcome. A guard parses every
  source under `src/gobby/hooks` and fails on any read of the stored attribute: an attribute
  load on any object, or a `getattr` with the literal name. A store, the annotation and a
  mention in a docstring or comment are not reads. A reader of the private attribute outside
  `src/gobby/hooks` is outside the guard.

Each case may carry `setup` deliveries, run with recording off, that bring the session to the
state the case needs. A setup delivery inherits the case's other keys but never the case's own
`payload` or `event`, so it delivers exactly what it names.

The loader requires every delivery, the case's own and each setup delivery, to start from a
non-empty native provider payload with a `source` the route's adapter table holds and a
`hook_type` that adapter's `EVENT_MAP` maps. Only an event type no routed adapter produces is
delivered as `event` with a `reason`, and no delivery carries both. A setup entry that
delivers an event carries its own `reason`; it does not inherit the case's. The `git` adapter has no
`EVENT_MAP`, so no pipeline case can deliver through it; none does, and the adapter corpus
covers it. `expect` holds the event type, the non-default response fields, the
adapter's native output, the collaborator reads in the order they ran and, when the delivery raised, the
exception type. Placeholders `<project>` and `<machine>` are filled at replay time with a
temporary checkout and the hub's pinned machine id; outputs are scrubbed of timestamps, UUIDs
and host paths.

Each replay runs in an isolated schema on the test hub, with the machine id pinned and the
daemon health check reporting ready. A webhook endpoint answers through an in-process
transport.

## Synthetic only

Every rule name starts with `synthetic-`. No case names, loads or asserts a bundled rule,
agent, pipeline or workflow.

## Replay

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_pipeline_corpus.py -q
```

Besides the replay, each event case is replayed with its handler broken, and each scenario with
its own path broken (the rule pass, the blocking-webhook pass, the deferred activation, the
session lookup); the outcome must change, and a scenario decided before the handler must not
change when only the handler is broken.

## Re-record

A deliberate pipeline change is re-recorded, not hand-edited. Set `GOBBY_CORPUS_RECORD=1` and
run the re-record test; it writes the observed blocks back into the case files:

```bash
GOBBY_CORPUS_RECORD=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_pipeline_corpus.py -k rerecord_committed_corpus
```

Review the diff before committing. A re-record that changes an expect block is a behavior
change and needs its own rationale.
