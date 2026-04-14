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

As part of execution, create an out-of-scope follow-up `gobby-tasks` task to align the
`gsqz` installer fallback chain with `gcode`'s installer model.

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

Before implementation begins, create a separate `gobby-tasks` follow-up task for later
work to align `gsqz` installation behavior with `gcode`.

That task should capture:

- review `gsqz` fallback precedence against `gcode`
- normalize installer precedence, version-stamp behavior, skip/upgrade semantics, and
  PATH handling where no documented reason to differ exists
- keep this work explicitly out of scope for the hook ownership/integration implementation

### 1. Install: Backup and Claim, No Semantic Migration

Change `gobby install` for supported hook surfaces so it:

- detects pre-existing non-Gobby hook files or entries
- writes dated backups before any modification
- records backup metadata and ownership claims in a Gobby manifest
- installs Gobby as the sole active owner for the supported events
- tells the user that prior hook config was backed up and future hook-based apps should be
  installed or reinstalled after Gobby is in place

Install does not attempt semantic migration of third-party hooks.

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

### 3. Detect Foreign Modification as Anti-Corruption, Not Coexistence

Add daemon-side monitoring or reconciliation for supported hook config locations.

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

- ownership manifest schema with baseline fingerprints
- preserved foreign modification artifact schema
- pending conflict and migration report schema
- integration/provider registry for known tools
- baseline backend state for reversible replacements like RTK vs `gsqz`

Expose operations for:

- install with backup-and-claim
- inspect ownership integrity
- inspect preserved foreign modifications
- view pending conflict or migration offers
- approve a migration
- remove an integration and revert to baseline
- restore from backup if necessary

## Test Plan

- `gobby install` detects existing foreign hook config, backs it up, records ownership, and
  claims sole ownership
- install is idempotent and does not duplicate Gobby-owned entries
- daemon detects post-install third-party modification of Gobby-owned hooks
- daemon preserves foreign content and restores the Gobby-owned baseline immediately
- known tools like RTK produce a pending migration offer from the preserved foreign config
- unknown tools produce a pending conflict record without false migration claims
- RTK migration imports supported behavior while Gobby remains the sole active hook owner
- removing RTK integration restores the recorded Gobby baseline, including `gsqz` where
  applicable
- doctor/report surfaces show ownership integrity, preserved foreign artifacts, pending
  migrations, active integrations, and active baseline backend
- Phase 0 follow-up task creation is completed before implementation begins, while the
  installer fallback-chain alignment itself remains out of scope

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
