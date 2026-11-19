# Adapter corpus

This directory holds the committed contract for the hook adapters: the translation from each
provider's native hook payload into a `HookEvent`, and from a `HookResponse` back into the
provider's output.

- `manifest.json` names the schema version, the case kinds, the sources and the platform
  record.
- `cases/<source>.json` (claude, codex, droid, grok, agy) hold two kinds of case:
  - `inbound`: a native payload for one hook type of the adapter's `EVENT_MAP`, run through
    `translate_to_hook_event`. Every hook type of every adapter has one. `expect.event` is the
    event the adapter built.
  - `outbound`: a `HookResponse` (`response`) for one hook type, run through
    `translate_from_hook_response`. Each hook type in a source's capability table has a case
    that sets every field the table declares for it, and each hook that declares
    `permission_decision` also has a deny case. `system_message` is set on many cases;
    `modify_args`, which no capability row declares, is set on
    `agy-out-PreToolUse-modify-args`, whose output shows that AGY drops it. Only the abstract
    ACP base adapter writes `modify_args` into its output (for `BeforeModel` and
    `BeforeToolSelection`); its `source` raises, and the hooks route builds no adapter that
    reaches that branch, so no routed adapter emits the field. `expect.output` is the provider
    output.
  Inbound payloads use the field names of the captured payloads in
  `tests/fixtures/provider_contracts/<source>/` where a capture exists for the hook type.
- `cases/node_origin.json`: one payload per node-origin branch that skips a hub-side file
  read (the spill reader in the claude, grok and codex adapters, the droid permission-wait and
  idle-cancel transcript checks, and the AGY prompt backfill). Each is replayed inside a node
  delivery (`delivery_scope` with a machine id that is not the hub's) and names a file that does
  not exist (`file_present: false`).
- `cases/git.json`: the payload the installed reference-transaction guard posts (hook type
  `PreToolUse`, `hook_event_name` `PreToolUse`, `tool_name` `Bash`, `tool_input.command`
  `git commit`, the staged paths and the integration branch) run through `GitAdapter`, then an
  `allow` and a `block` verdict sent back out.
- `cases/refusals.json`: requests posted to the hooks route of a fresh app. An unknown source
  and an envelope without `hook_type` are refused with status 400 before any adapter
  translation runs.

`expect` blocks are scrubbed: timestamps, UUIDs and host paths become placeholders, so a
replay on another machine or day yields the same block.

## Synthetic only

Every payload is synthetic. Paths sit under `/srv/`; no case carries a home directory or a
real transcript path.

## Replay

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_adapters_corpus.py -q
```

Besides the replay, each case is replayed again with its production path broken (the
translation method, or for a node-origin case the hub-side reader and the `node_origin`
binding), and the outcome must change.

## Re-record

A deliberate adapter change is re-recorded, not hand-edited. Set `GOBBY_CORPUS_RECORD=1` and
run the re-record test; it writes the observed blocks back into the case files:

```bash
GOBBY_CORPUS_RECORD=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_adapters_corpus.py -k rerecord_committed_corpus
```

Review the diff before committing. A re-record that changes an expect block is a behavior
change and needs its own rationale.
