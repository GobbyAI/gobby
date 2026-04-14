# Anti-Corruption Hook Ownership With RTK Example

## Summary

Make hook ownership protection the primary goal.

After `gobby install`, Gobby becomes and remains the sole owner of supported hook
surfaces. Third-party hook installers are not allowed to take effective ownership back.
If a tool like RTK rewrites hook config later, Gobby treats that as a detected conflict
against Gobby-owned state, preserves the foreign config for analysis, restores or
reasserts the Gobby-owned baseline, and then offers a migration into the Gobby model.

Initial install remains simple:

- detect existing hooks
- back them up
- claim ownership
- tell the user future hook-based tools should be installed or reinstalled after Gobby is
  in place

Migration remains daemon-driven and explicit:

- detect foreign writes
- preserve them
- keep Gobby ownership intact
- offer integration for known tools like RTK

Before implementation begins, the PR author or feature owner must create and reference an
out-of-scope `gobby-tasks` Phase 0 follow-up for `gsqz`/`gcode` alignment. Current
follow-up: `#11752`. If that step is missed before PR creation, assign a blocking
post-merge follow-up to the project lead, notify reviewers, and add the task ID to
Section 0 and the implementation PR before the hook-immutability work is considered
complete.

## RTK User Stories

### Gobby User Wanting RTK After Gobby Is Installed

- As a Gobby user, I want Gobby to own my hook surfaces after install and keep them
  stable.
- As a Gobby user, I want to install RTK normally afterward without risking silent
  corruption of Gobby hook config.
- As a Gobby user, I want Gobby to detect RTK's hook changes immediately and prevent RTK
  from taking over the active hook path.
- As a Gobby user, I want Gobby to preserve RTK's written config so it can offer an
  integration instead of losing what RTK tried to configure.
- As a Gobby user, I want RTK to become active only after Gobby has imported supported
  RTK behavior into the Gobby hook pipeline.
- As a Gobby user, I want removing the RTK integration to restore the prior Gobby
  baseline behavior, including `gsqz` where that is the recorded baseline.

### Existing RTK User Installing Gobby

- As an existing RTK user, I want `gobby install` to detect and back up my existing
  RTK-managed hook config before Gobby claims ownership.
- As an existing RTK user, I want Gobby to tell me clearly that the prior hook config was
  preserved and that future RTK installs will be detected and offered for migration.
- As an existing RTK user, I do not want Gobby install to guess at RTK semantics or
  silently merge unsafe hook definitions.
- As an existing RTK user, I want a later RTK reinstall to be treated as a detected
  foreign modification, not as valid competing ownership.
- As an existing RTK user, I want a rollback path that returns me to stable Gobby-owned
  hooks plus the prior Gobby baseline behavior if I remove the RTK integration.

## Implementation Changes

### 0. Phase 0 Follow-Up Task

Before implementation begins, the PR author or feature owner creates a separate
`gobby-tasks` follow-up task for later work to align `gsqz` installation behavior with
`gcode`.

Phase 0 ticket reference: `#11752`

That task must capture:

- review `gsqz` fallback precedence against `gcode`
- normalize installer precedence, version-stamp behavior, skip/upgrade semantics, and
  PATH handling where no documented reason to differ exists
- keep this work explicitly out of scope for the hook ownership/integration implementation

Process requirements:

- the task title must contain `gsqz/gcode alignment`
- the task ID must be referenced in this document and in the hook-immutability
  implementation PR
- the task body must explicitly state that the alignment work is out of scope for hook
  ownership and integration behavior
- at least one reviewer must verify the task exists before approving the implementation PR

Reviewer checklist item:

- `Phase 0 ticket created: TASK-ID referenced`
- verify the linked ticket title contains `gsqz/gcode alignment`
- verify the linked ticket explicitly says it is out of scope for hook
  ownership/integration work

### 1. Install: Backup and Claim, No Semantic Migration

Change `gobby install` for supported hook surfaces so it:

- runs `validateInstall()` before any filesystem mutation to confirm writable hook
  targets, writable manifest and staging directories, sufficient disk space for staged
  backups plus manifests, and the ability to acquire the install lock
- exposes `dryRunInstall()` through `gobby install --dry-run` so the user can see the
  detected hook surfaces, planned backup paths, ownership claims, and required free space
  without changing active config
- executes detect -> backup -> claim -> install inside a transaction-like
  `performInstall()` wrapper so the sequence is atomic from the user's perspective
- stages backups, manifests, and rendered hook payloads under
  `~/.gobby/hooks/staging/install-<timestamp>-<pid>/` and only swaps them into live
  locations after every staged artifact has been validated
- detects pre-existing non-Gobby hook files or entries, writes dated backups before any
  ownership claim, records backup metadata and ownership claims in the manifest, and then
  installs Gobby as the sole active owner for the supported events
- tells the user that prior hook config was backed up, whether the install committed or
  rolled back, and that future hook-based apps should be installed or reinstalled after
  Gobby is in place

Install does not attempt semantic migration of third-party hooks.

Implementation expectations for the install path:

- `detectExistingHooks()` retries transient filesystem and lock-contention failures with a
  short bounded backoff before the install is aborted as a real detection failure
- `createBackup()` writes dated backups, verifies the staged backup checksum, and retries
  transient write failures before returning a fatal backup error
- `writeManifest()` writes a staged manifest, validates the schema and integrity
  fingerprints, fsyncs the file, and only then participates in the final atomic swap
- `claimOwnership()` is not treated as committed until the backup, manifest, and active
  hook payload swaps all succeed
- `installGobbyHooks()` writes only to staged targets until `performInstall()` reaches the
  final atomic commit step
- `rollbackInstall()` restores the latest dated backups, removes staged files, reverts any
  staged ownership claims, and writes a rollback entry to trace history if
  `claimOwnership()`, `installGobbyHooks()`, or `writeManifest()` fails or the process is
  interrupted

User-facing failure handling:

- exit `2`: preflight validation or dry-run failed; no changes were made
- exit `3`: backup creation could not be completed; no ownership claim or active install
  may remain
- exit `4`: atomic commit failed but rollback completed successfully
- exit `5`: atomic commit failed and rollback was incomplete; manual recovery is required
- error messages must name the failing step, the affected path, whether rollback ran, and
  the backup or manifest path the user should inspect next

### 2. Add Ownership Manifest and Protected Baseline

Persist a manifest that defines the Gobby-owned hook baseline and is treated as the source
of truth.

Manifest should track:

- claimed surfaces and events
- exact files or config sections Gobby owns
- integrity fingerprints for Gobby-owned content
- backup paths and timestamps
- known foreign fingerprints seen at install time
- current integrated providers
- current reversible baseline backend for affected behaviors such as `gsqz`

This baseline is what the daemon protects and reasserts.

The initial design is user-scoped, not system-wide. The canonical manifest lives at
`~/.gobby/hooks/ownership-manifest.v1.json`, its automatic dated backups live under
`~/.gobby/hooks/backups/<timestamp>/`, and preserved foreign payloads live under
`~/.gobby/hooks/preserved/<conflict-id>/`.

### 3. Detect Foreign Modification as Anti-Corruption, Not Coexistence

Add daemon-side monitoring or reconciliation for supported hook config locations.

Monitoring triggers:

- file watcher events on supported hook files or directories whenever the platform can
  subscribe reliably
- periodic polling every 30 seconds with jitter while the daemon is healthy
- exponential backoff polling up to 5 minutes when the watcher or filesystem reports
  repeated transient errors
- manual on-demand checks through `gobby hooks doctor` or `gobby hooks status
  --ownership`

When hook config differs from the manifest baseline:

- classify whether the Gobby-owned region or file was modified
- treat the change as foreign modification or corruption unless it matches an approved
  Gobby operation
- capture the foreign content into a preserved conflict artifact
- restore or rewrite the active config back to the Gobby-owned baseline
- record a pending integration candidate if the foreign content matches a known tool like
  RTK

The key rule:

- Gobby never allows third-party hook ownership to remain active after detection

An approved Gobby operation is one that can be attributed to all of the following:

- an active lockfile for install, restore, or migration work
- a tracked PID or operation ID owned by the current daemon process
- a matching operation-log entry that records the expected source fingerprint and target
  fingerprint transition

Race and persistence handling:

- when concurrent writes are detected, Gobby assigns a unique conflict ID, writes a
  preserved conflict artifact first, and then retries baseline restoration with
  exponential backoff from 250 milliseconds up to 4 seconds
- if another foreign write lands during restore, the daemon preserves the new payload,
  appends the event to trace history, and treats the latest payload as the candidate for
  classification and migration reporting
- if a persistent foreign daemon keeps reapplying config, Gobby continues restoring the
  protected baseline, throttles repeated notifications, and records the source as a
  persistent foreign writer rather than silently allowing coexistence
- preserved foreign content that matches a known tool can be promoted into a pending
  integration candidate; unknown content remains preserved-only and reportable

### 4. Offer Explicit Migration From Preserved Foreign Config

Once the daemon has secured Gobby ownership again, it generates a migration offer.

Migration flow:

1. detect foreign modification
2. preserve the foreign config payload and metadata
3. restore Gobby-owned active hook state
4. classify known tool if possible
5. produce a migration report describing supported and unsupported semantics
6. wait for explicit approval
7. import supported behavior into a Gobby-managed integration/provider
8. record the integration in the manifest and trace history

Unknown foreign tools:

- preserve and report
- no semantic import
- no silent approximation

Approval and queue lifecycle:

- the explicit approval path is `gobby hooks offers approve <offer-id>` or the equivalent
  UI action `hooks-offer-approve`; there is no config flag for silent auto-approval
- every migration offer persists until accepted, rejected, superseded, or it expires
  after 30 days; expiry keeps the preserved artifact and trace history but removes the
  active approval prompt
- notification retries for pending offers back off from 5 minutes to 30 minutes, 6
  hours, and then daily until the offer is resolved or expires
- multiple pending migrations are queued FIFO by detection time and surfaced through
  `gobby hooks offers --pending`; approval defaults to the oldest pending offer only when
  exactly one exists, otherwise the user must choose an explicit offer ID
- `gobby hooks offers select <offer-id>` is allowed as a UI and CLI helper to pin one
  pending offer as the current review target without approving it
- `gobby hooks offers reject <offer-id>` records an explicit rejection for that preserved
  fingerprint, keeps the preserved artifact and migration report, and suppresses further
  prompts unless new foreign content produces a different fingerprint
- `gobby hooks offers dismiss <offer-id>` hides the current prompt but leaves the
  migration offer pending and visible in listings
- if a foreign tool is reinstalled while a migration offer is pending, Gobby does not
  merge the new install into the old offer; it preserves the new payload, marks the old
  offer superseded, generates a fresh migration report, updates the manifest to point to
  the new pending offer, and appends both events to trace history

### 5. Implement RTK as a Known Migration Path

RTK support is a dedicated migration module, not a generic hook merge.

RTK-specific requirements:

- detect RTK signatures in the preserved foreign hook config
- import supported RTK semantics into a Gobby-managed RTK integration/provider
- explicitly report unsupported RTK semantics
- keep an audit trail of the imported values and source files
- never let RTK remain the active installed hook owner once detected

For the current `gsqz` replacement behavior:

- successful RTK migration makes RTK the active Gobby-managed implementation for the
  relevant path
- removing RTK integration restores the prior Gobby baseline behavior
- that baseline defaults to `gsqz` unless another Gobby-managed replacement was already
  recorded

Supported RTK semantics must be defined narrowly and explicitly.

Supported RTK scope for migration:

- `createSlice(...)` definitions, including slice name, reducer map, and statically
  declared extra reducers that can be resolved without executing runtime code
- `configureStore(...)` reducer wiring and static `middleware` composition that uses
  array literals or `getDefaultMiddleware().concat(...)`
- `createAsyncThunk(...)` declarations imported from `@reduxjs/toolkit` when their source
  modules are part of the detected migration set
- statically exported selectors from migrated slice modules and simple
  `createSelector(...)` wrappers that only reference migrated slices
- Gobby-managed provider registration and removal flow for the migrated RTK integration,
  including restoration to the recorded baseline such as `gsqz`

Unsupported RTK scope for migration:

- custom enhancer hacks or store wrappers that bypass `configureStore(...)`
- non-serializable, environment-dependent, or runtime-generated middleware and ad-hoc
  runtime code
- external scripts, shell hooks, or binaries that must be executed to recover semantics
- dynamic imports, eval-based configuration, or reducer composition that requires running
  arbitrary code to understand
- semantics outside the supported version range or outside the detected source roots

Detection method:

- candidate discovery uses filename patterns such as `**/store.{js,jsx,ts,tsx}`,
  `**/*Slice.{js,jsx,ts,tsx}`, and `**/features/**/*.{js,jsx,ts,tsx}`
- candidate triage uses content regexes for `createSlice(`, `configureStore(`,
  `createAsyncThunk(`, `getDefaultMiddleware(`, and `@reduxjs/toolkit`
- AST matching is authoritative and must confirm the relevant imports and call
  expressions before anything is accepted as supported migration input

Version and external-reference policy:

- only `@reduxjs/toolkit` versions `>=1.9` and `<3` are eligible for semantic migration
- unsupported versions are preserved and reported, not approximated
- migration may follow statically imported local source files under the project root or
  recorded install root, but it must not execute foreign scripts or follow opaque
  external references
- any external reference that cannot be resolved statically is recorded in the migration
  report as unsupported and kept in preserved artifacts for audit

The RTK migration flow, preserved foreign hook config record, Gobby-managed RTK
integration/provider state, and reversible baseline behavior such as `gsqz` must all
reference this supported-scope definition so the implementation and audit trail can
explicitly accept, reject, or report unsupported semantics together with the source
files that produced them.

### 6. Surface Conflicts Immediately, Not Only Through Doctor

The daemon must not rely on the user discovering conflicts later.

Required surfacing:

- immediate active notification when a session or UI channel exists
- persistent pending conflict or migration record
- `gobby hooks doctor` as the audit surface
- explicit migrate command or approval action to accept integration

Required message semantics:

- a foreign tool modified Gobby-owned hooks
- Gobby restored protected ownership
- the foreign tool is not active yet
- a migration or integration is available if supported

### 7. Keep the Generic Layer Narrow

Generic infrastructure should cover:

- ownership claims
- baseline fingerprinting
- conflict detection
- backup and preserved foreign artifacts
- migration orchestration
- provider lifecycle and precedence

Tool semantics are not assumed generic. Known tools get dedicated migration logic.
Unknown tools get preservation and reporting only.

## Public Interfaces and Types

Add or formalize:

- `gobby.hooks.ownership-manifest.v1` as canonical JSON at
  `~/.gobby/hooks/ownership-manifest.v1.json`
- `gobby.hooks.preserved-conflict.v1` as canonical JSON metadata plus preserved payload
  files under `~/.gobby/hooks/preserved/<conflict-id>/`
- `gobby.hooks.migration-offer.v1` and `gobby.hooks.migration-report.v1` as canonical
  JSON records for approval state and semantic analysis
- `gobby.hooks.providers.v1` for known integration/provider registry state
- `gobby.hooks.trace-history.v1` for durable install, restore, rollback, supersession,
  and approval history

Ownership manifest details:

- on-disk format: JSON only, schema name `gobby.hooks.ownership-manifest.v1`
- manifest location: `~/.gobby/hooks/ownership-manifest.v1.json`; system-wide manifests
  are out of scope for this design
- required fields: `schema_version`, `manifest_fingerprint`, claimed surfaces, managed
  regions, integrity fingerprints, backup paths and timestamps, known foreign
  fingerprints, active providers, reversible baseline backend such as `gsqz`, pending
  offer IDs, and last successful restore state
- corruption detection: the loader validates JSON shape, `schema_version`, and a
  checksum-style integrity fingerprint over canonicalized manifest content before the
  manifest is trusted
- backup behavior: every successful install, migration, restore, or schema migration
  writes a dated manifest backup under `~/.gobby/hooks/backups/<timestamp>/`; retain at
  least the most recent 10 snapshots and any snapshot still referenced by an unresolved
  migration offer or trace-history entry
- restore behavior: `gobby hooks restore --backup <backup-id>` restores both the manifest
  and the associated hook payloads from the selected dated backup
- deleted or out-of-sync recovery: quarantine corrupt manifests under
  `~/.gobby/hooks/corrupt/`, attempt recovery from the newest valid dated backup, then
  reconcile the recovered manifest against actual hook content; if reconciliation is not
  safe, fall back to the protected Gobby baseline and recorded reversible backend such as
  `gsqz`
- schema evolution: `schema_version` is required, same-major upgrades may migrate in
  place after writing a backup, newer-major manifests are read-only and must not be
  downgraded in place, and any failed migration restores the prior dated backup

Canonical operations and API surface:

- `gobby install --dry-run` and programmatic `hooks.dry_run_install()`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  reports detected surfaces, backup destinations, permission checks, disk-space checks,
  and the reversible baseline that would be recorded
- `gobby install` and programmatic `hooks.perform_install()`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  runs atomic detect -> backup -> claim -> install semantics with the exit codes defined
  in Section 1
- `gobby hooks status --ownership` and programmatic
  `hooks.status(view="ownership")`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  reports ownership integrity and baseline fingerprint status only
- `gobby hooks status --preserved` and programmatic
  `hooks.status(view="preserved")`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  lists preserved foreign artifacts, source guesses, timestamps, and related migration
  report IDs; it does not perform ownership validation
- `gobby hooks status --integrations` and programmatic
  `hooks.status(view="integrations")`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  lists active Gobby-managed integrations/providers and the currently recorded baseline
  backend such as `gsqz`
- `gobby hooks offers --pending` and programmatic
  `hooks.list_offers(state="pending")`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  lists pending migration offers in FIFO order with TTL, selection state, and related
  migration report summaries
- `gobby hooks offers approve <offer-id>`,
  `gobby hooks offers reject <offer-id>`,
  `gobby hooks offers dismiss <offer-id>`, and
  `gobby hooks offers select <offer-id>` with matching programmatic API methods:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  these commands mutate offer state and record the action in manifest-linked trace
  history
- `gobby hooks doctor` and programmatic `hooks.doctor()`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  provides the audit surface that joins ownership state, preserved artifacts, pending
  offers, migration reports, active integrations, and baseline backend state
- `gobby hooks integrations remove <provider>` and programmatic
  `hooks.remove_integration(provider)`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  removes a Gobby-managed provider and reverts to the recorded reversible baseline
- `gobby hooks restore --backup <backup-id>` and programmatic
  `hooks.restore_from_backup(backup_id)`:
  CLI plus programmatic API; human-readable output by default and JSON with `--json`;
  restores the selected dated backup and reports whether the manifest, active hook
  payloads, and baseline backend were fully restored

## Test Plan

- install success path:
  set up an existing foreign hook config, run `gobby install`, tear down by restoring the
  dated backup, and assert the doctor or report output, manifest backup metadata, active
  Gobby baseline, and recorded reversible backend such as `gsqz`
- install idempotency:
  run `gobby install` twice against the same already-managed surface, tear down by
  removing staged temp data, and assert no duplicate Gobby-owned entries, no duplicate
  ownership claims, and a stable manifest fingerprint
- preflight validation and dry-run:
  simulate missing permissions, missing lock acquisition, and insufficient disk space,
  run `gobby install --dry-run`, tear down by restoring permissions and temp space, and
  assert exit `2`, no manifest mutation, no backup creation, and a human plus JSON report
  explaining the failure
- backup creation failure:
  make the backup target unavailable, run install, tear down by restoring the backup path,
  and assert exit `3`, cleaned staging artifacts, no committed ownership claim, and a
  preserved doctor or report entry for the failure
- atomic commit or disk-full during restore:
  force failure during the final swap or restore write path, tear down by clearing the
  injected fault, and assert rollback behavior, cleanup of staged files, retry semantics,
  the correct exit code (`4` or `5`), and explicit reporting of whether the Gobby
  baseline and manifest were fully recovered
- manifest corruption recovery:
  corrupt or delete `ownership-manifest.v1.json`, trigger daemon load or `gobby hooks
  doctor`, tear down by restoring the latest valid backup, and assert quarantine of the
  bad manifest, recovery from backup or fallback to the protected baseline, preserved
  pending migrations, and correct `gsqz` baseline reporting
- daemon race with foreign modification during restore:
  simulate a foreign tool rewriting hook config while the daemon is restoring, tear down
  by stopping the writer and restoring the stable baseline, and assert preserved conflict
  artifacts with unique conflict IDs, reconciliation retries with backoff, stable active
  Gobby ownership, and a pending migration offer or conflict record for the newest
  foreign payload
- persistent foreign daemon behavior:
  simulate a process that repeatedly reapplies foreign config, tear down by stopping it,
  and assert throttled notifications, trace-history entries for repeated restores, and a
  persistent-writer report rather than silent coexistence
- known versus unknown migration classification:
  feed one preserved RTK payload and one unknown foreign payload, tear down by removing
  the pending offers, and assert the RTK path produces a migration offer and migration
  report while the unknown path produces preservation-only reporting
- approval flow accept, reject, and dismiss:
  create pending migration offers, exercise approve, reject, and dismiss actions, tear
  down by removing the offers and restoring baseline state, and assert state transitions,
  preserved artifacts, trace-history entries, and whether prompts reappear as expected
- multi-conflict queue handling:
  generate concurrent foreign writes from multiple tools, tear down by restoring the
  protected baseline, and assert FIFO pending-offer ordering, explicit selection behavior,
  separate migration reports, and distinct preserved artifacts for each conflict
- RTK supported and unsupported semantics:
  provide RTK source files that include supported `createSlice`, `configureStore`, and
  `createAsyncThunk` usage plus unsupported enhancer hacks or dynamic middleware, tear
  down by removing the integration, and assert the migration report records accepted and
  rejected semantics with source files while the active Gobby-managed provider keeps sole
  ownership
- integration removal:
  migrate RTK successfully, remove the integration, tear down by confirming no active RTK
  provider remains, and assert the manifest and doctor surfaces show reversion to the
  recorded baseline, including `gsqz` where applicable
- schema evolution and upgrade path:
  load older manifest snapshots and a simulated newer-major manifest, tear down by
  restoring the latest supported schema backup, and assert successful same-major
  migrations, safe refusal of unsupported newer-major manifests, preserved backups, and
  trace-history records for schema migration attempts
- performance and monitoring overhead:
  measure idle watcher plus polling overhead and restore latency under repeated conflict
  scenarios, tear down by stopping the monitor harness, and assert the daemon overhead is
  within an agreed budget while doctor or report artifacts still capture pending
  migrations, preserved payloads, baseline state, and retry counts
- Phase 0 follow-up verification:
  confirm before implementation and review that the linked `gobby-tasks` item exists,
  that its title contains `gsqz/gcode alignment`, that it is referenced in this document
  and the PR, and that it explicitly marks installer alignment as out of scope

## Assumptions and Defaults

- Assumption: third-party hook tools like RTK will not adapt to Gobby.
- Default: `gobby install` backs up existing non-Gobby hook config and immediately claims
  hook ownership.
- Default: after install, Gobby does not permit third-party hook ownership to remain
  active on protected surfaces.
- Default: foreign hook writes are treated as conflicts plus possible migration
  candidates, not as valid coexistence.
- Default: the daemon restores Gobby-owned baseline hook state before offering migration.
- Default: migration is explicit and user-approved, not automatic.
- Default: semantic migration is tool-specific, not fully generic.
- Default: removing a migrated integration reverts to the prior Gobby baseline behavior,
  with `gsqz` as the fallback where it was the recorded baseline.
