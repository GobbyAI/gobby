# Isolated Claude and Codex composer proof

This is an opt-in acceptance fixture for #22915, confirmed-empty composer delivery.
Default pytest collection skips it before setting up a daemon or accessing auth.
Implementation and a passing unit suite do not establish live acceptance.

## Admission

Execution needs a separate Orchestrator grant for the independently reviewed
fixture commit and tree, plus the Lane Manager's runtime admission. Keep the task
open until the remaining live criteria and close release are satisfied.

Use only the two disposable proof processes. Never target an ordinary roster
terminal, an operator draft, or a production daemon. Obtain a fresh complete
exclusion map from the Assistant, including all roster session and terminal UUIDs.
The admission model requires at least 26 unique UUIDs and a map issued within five
minutes. The size check cannot prove completeness; the named message and grant
must attest the actual complete map. An exclusion map grants no terminal access.

Before execution, independently verify the clean checkout's exact commit/tree,
installed native set, provider binaries, and support executables. The fixture
hashes executable bytes and verifies the existing gcode/gdaemon/ghook identity
stamp; it never builds, promotes, installs, or restarts production binaries.
Use the installed byte hashes, because signing changes cargo artifact bytes.

For a worktree source, announce the isolated test exception and include the
announcement message UUID. The fixture sets `GOBBY_ALLOW_WORKTREE_DAEMON=1` only
inside its sealed subprocess environment. This is not production activation.

## Private configuration

The consumer is `tests.e2e.composer_proof_admission.Admission`, an extra-forbid
Pydantic model. Write a new owner-only `0600` JSON file outside the repository.
Create an empty owner-only `0700` artifact directory. Supply references to existing
owner-private credential files, never token values. Replace every placeholder in
this template; it is intentionally invalid until populated with real admissions.

```json
{
  "grant": "PD_AUTHORIZED_ISOLATED_EXECUTION",
  "reviewed_commit": "<40-character independently reviewed fixture commit>",
  "reviewed_tree": "<40-character reviewed tree>",
  "issued_at": "<fresh UTC ISO-8601 timestamp>",
  "exclusion_message_id": "<Assistant complete-map message UUID>",
  "complete_exclusion_map": true,
  "excluded_identities": ["<every session/terminal UUID in the complete map>"],
  "native": {
    "gcode": {"path": "/absolute/admitted/bin/gcode", "sha256": "<64 hex>"},
    "gdaemon": {"path": "/absolute/admitted/bin/gdaemon", "sha256": "<64 hex>"},
    "ghook": {"path": "/absolute/admitted/bin/ghook", "sha256": "<64 hex>"},
    "gterm": {"path": "/absolute/admitted/bin/gterm", "sha256": "<64 hex>"},
    "gclient": {"path": "/absolute/admitted/bin/gclient", "sha256": "<64 hex>"}
  },
  "providers": {
    "claude": {
      "path": "/absolute/admitted/claude", "sha256": "<64 hex>",
      "auth_reference": "/absolute/private/claude/.credentials.json"
    },
    "codex": {
      "path": "/absolute/admitted/codex", "sha256": "<64 hex>",
      "auth_reference": "/absolute/private/codex/auth.json"
    }
  },
  "support_tools": [
    {"path": "/absolute/admitted/node", "sha256": "<64 hex>"}
  ],
  "artifact_dir": "/absolute/private/proof-evidence",
  "worktree_test_notice_id": "<isolated-test announcement UUID>"
}
```

All native paths must share the admitted directory. Include every executable
needed by provider wrappers in `support_tools`; these paths supply the sealed
PATH alongside system utilities. The source interpreter supplies hook/MCP Python.
No API keys, inherited provider homes, parent terminal identities, or production
database overrides are forwarded. Temporary bootstrap configuration selects the
test hub and a fresh PostgreSQL schema; runtime config is seeded in that schema.

The test installs hooks only under its private temporary project and HOME.
Auth files are symlink references, not copies. Claude's config directory and
Codex's home are private fixture paths. File-backed auth may still require
onboarding or trust setup; the driver refuses any surface that cannot register
through actual hooks and reach a naturally idle, positively empty composer.
It does not automate login, trust dialogs, approval screens, or auth recovery.

On macOS, Claude normally stores credentials in Keychain, with configuration
directory scoped entries; its supported private-file fallback is conditional.
An existing operator Keychain login alone does not prepare the isolated directory.
See [Claude authentication](https://code.claude.com/docs/en/authentication).
Admit a prepared private credential file explicitly. Provider refresh may write
through or replace an auth reference, so the execution grant must cover refresh
of that exact reference. Credential state is never exported as proof evidence.

Claude starts with built-in and MCP tools disabled. Codex starts with a read-only
sandbox and approval policy `never`. Both receive a fixed prompt to answer READY
and acknowledge notices briefly, without edits or tools. Unsupported flags or
unprepared onboarding refuse the proof; there is no permissive fallback. See
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
[Codex CLI reference](https://developers.openai.com/codex/cli/reference/).

## Execute only after admission

From the reviewed clean checkout, with the private admission file populated:

```bash
RTK_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 \
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
GOBBY_TEST_PROTECT=1 \
GOBBY_COMPOSER_PROOF_EXECUTION=PD_AUTHORIZED_ISOLATED_EXECUTION \
GOBBY_COMPOSER_PROOF_ADMISSION=/absolute/private/admission.json \
uv run pytest tests/e2e/test_composer_live_proof.py -q --no-cov --tb=short -p no:cacheprovider
```

Never set the opt-in variables during normal unit validation. The whole matrix
has a 28-minute bound, excluding fixture setup and bounded cleanup. It reuses the
existing isolated e2e daemon, native host, public terminal WebSocket and MCP tools.
The two real provider sessions bind through their real hooks. Binding requires
the exact provider/session/terminal/project/host epoch and disjoint exclusion map.
Frame text and cursor come from the same full native semantic frame.

## Required matrix and evidence

For each provider, the order is:

1. Empty composer, normal notice: actual text and Enter, actual prompt-submit
   hook, natural idle/empty completion, unique durable message readback.
2. Owned draft, normal notice: the exact synthetic draft with cursor five cells
   left from its end; zero automatic bytes; unchanged text/cursor; durable notice;
   actual initial and 15/30/60-second withheld attempts within 130 seconds.
   Restore the owned draft using Right five and exact Backspace count only.
   Require actual retry submission and durable readback within 250 seconds.
3. Repeat occupied preservation/recovery for an urgent notice.
4. Empty composer, urgent notice: the same real submission and durability checks.

Drafts are `R2_22915_CLAUDE_DRAFT_ABCD_EFGH` and
`R2_22915_CODEX_DRAFT_ABCD_EFGH`. The fixture checks exact text and cursor before
using 31 or 30 Backspaces. No interrupt, Enter, Escape, Ctrl-C, Ctrl-L, or generic
drain is used for editor cleanup. Normal and urgent wakes require positive empty
reads under Josh's 2026-09-29 decision (memory `720f1129`). Unknown/error states
must withhold input; their source regressions remain separate from this matrix.

Test-only instrumentation wraps the actual coordinator dispatch and dispatcher
retry outcome. It does not replace their behavior or manufacture lifecycle.
The active detection fingerprints observed inside the daemon must match the
isolated database registry used by the frame reader. Native fanout is refused;
the fixture admits direct-session notifications only.

Artifacts contain reviewed source identities, executable versions/hashes/inodes,
actual runner/interpreter/module paths and detection fingerprints, surface and
durable message UUIDs, hash-only frame/write receipts, cursor positions, retry
times/outcomes, submission-hook timestamps, and cleanup state. Raw frames, CLI
transcripts, auth, environment dumps and provider config are not exported.
A pytest failure or refusal cannot be reported as a passing criterion.

## Cleanup

Before any terminal kill, a private fixture-only handshake marks automatic input
quiescent, cancels and awaits the daemon's owned composer/deferred retry tasks,
and awaits in-flight wake locks. An unowned retry or changed binding refuses it.
Then reread each terminal's authoritative binding and epoch before sending the
existing public kill request. Close and await frame/WebSocket readers.

Whole-host shutdown requires the same host epoch and an empty host inventory.
Only after confirmed provider/host cleanup may the owned daemon process tree and
private HOME/socket state be removed. On uncertain creation, rebinding, startup,
or cleanup, preserve the isolated process/state and record their paths and PID
for the owner. Never signal an unknown target or run broad machine cleanup.
The test schema remains owned by existing e2e fixtures; no production state is used.

## Staged compact/clear criterion remains pending

This driver does **not** establish criterion 6. The trace gate matches the actual
command-plus-newline write. `LiveProof.held_boundary` requires that gate to remain
held, a fresh native frame showing the exact command, unchanged surface binding,
and no submission/compact/session-start hook since the supplied baseline. It
releases the gate and refuses if the real held frame is unavailable. The observer
never starts a race and is not called by the base wake matrix.

A Delivered receipt alone cannot show a held command: `submit_text` writes command
plus newline before delayed Enter, and a short `/compact` or `/clear` may already
submit from that newline. Synthetic snapshots or a timing-only pause cannot stand
in for the real frame.

A later extension must observe the actual held boundary through the real native
frame, then race a durable normal/urgent wake through the actual public handoff
path while the existing physical composer lock remains held. It must account for
clear successors, submission confirmation, failed handoff recovery, cancellation,
unique durable notice delivery, and ownership-safe cleanup. Until those outcomes
are observed for both providers, report criterion 6 PENDING and keep #22915 open.
Likewise, criterion 5 remains PENDING until this real matrix passes under admission.
