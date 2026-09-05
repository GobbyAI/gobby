# Retire the Legacy Gobby Wiki

Approved execution intent supplied by the user in session #11777. Removal epic:
#21771. Worktree: `/Users/josh/.gobby/worktrees/gobby/wt-epic-21771`, branch
`wt-epic-21771`, based on `0.5.0` at
`53e862d53d40027173ba2d74d86858d44fbc88f4`.

## 1. Outcome and execution boundaries

Remove the existing wiki implementation, content, integrations, and obsolete task
backlog. Complete retirement before planning the replacement from the user's saved
outline. Perform implementation, isolated validation, and commits in the dedicated
epic worktree. Main-checkout daemon operations occur after removal changes land.

Preserve canonical session summaries and revisions, handoffs, transcript archives,
memories, project records, original files outside wiki storage, gcode functionality,
indexing, search, graph data, shared platform services, and explicit grant reservations.

Archive the old implementation with an annotated Git tag before removing it from
the active tree. Retain a scoped recovery backup outside repository indexing and
active wiki discovery. Future wiki development receives a new plan and task tree.

## 2. Retire the existing backlog

Close these 38 tasks through gobby-tasks, recording that the user superseded the
implementation with complete retirement: #19670, #19664, #19665, #19671, #19672,
#18779, #18790, #18869, #19766–#19793 inclusive, #21504, and #21539.

Use no-work closure reason `obsolete`, preserve history and linked commits, and
verify ancestor closures. Recheck ownership immediately before mutations. The
removal epic is outside the old task tree.

Narrow #21577 node configuration and acceptance to gcode. Narrow #21586 daemon
orchestration migration to code-index services and shared dependencies. Archive
the active wiki-output-design plan through the registry. Update roadmap references
and obsolete active guidance. Closing #21504 leaves reserved grants unchanged;
the replacement's datastore contract belongs to subsequent planning.

**Acceptance:** Every listed old task is closed; the two shared tasks retain gcode
obligations; completed work remains historically accessible; obsolete wiki work
cannot be dispatched.

## 3. Remove code and public surfaces

First relocate independently consumed functionality: terminal-output redaction
from the session-wiki module into shared utilities (updating agent spawn and health
checks); hub request forwarding from the wiki package into the shared files layer
(preserving authentication, streaming, response handling, and proxy-loop protection
for chat attachments); Mermaid utilities consumed by gcode.

Remove the entire crates/gwiki package, workspace membership, release/build/install
wiring, and Gobby-owned installed gwiki binaries. Remove Python wiki gateways,
services, watchers, scheduling, project-purge dependencies, and wiki runtime contracts.
Remove wiki MCP registration, HTTP routes, CLI/setup features, configuration,
generation profiles, and associated authorization entries. Remove wiki UI, settings,
navigation, shortcuts, subscriptions, and API clients. Saved Wiki-tab selections
return to the application's normal default view.

Remove automatic overview injection, recap generation, session-summary file mirrors,
and mirror-freshness requirements. Canonical summary generation and transcript
processing continue independently. Remove obsolete wiki tests; retain and update
tests covering surviving shared behavior.

Retire dedicated wiki schema objects through a new migration and update schema and
configuration contract carriers. Preserve historical migrations and shared datastore
objects.

**Acceptance:** Wiki tools and routes disappear from discovery; removed HTTP
endpoints return the application's normal missing-route response. Startup,
installation, project cleanup, and session processing succeed without the wiki.

## 4. Purge all legacy wiki state

Implement one bounded retirement procedure using existing datastore clients and
operational mechanisms. Default operation produces an inventory; apply uses that
explicit inventory.

Inventory this repository's wiki and legacy vaults associated with other recorded
checkouts; all personal, topic, project, and test vaults under hub wiki home; imported
source copies, generated pages, manifests, exports, session mirrors, discovery
registries; PostgreSQL gwiki_documents, gwiki_chunks, gwiki_links, gwiki_sources,
gwiki_ingestions; wiki-owned Qdrant collections; dedicated FalkorDB graph gobby_wiki;
installed wiki rules, variables, jobs, and remaining writer processes.

Execute in order:

1. Record Git archive tag, exact deletion inventory, ownership evidence, and scoped
   recovery backup.
2. Rehearse deletion and restoration against isolated state.
3. Disable legacy injection and writers, drain outstanding wiki operations, and
   deploy removal changes through the main checkout.
4. Verify installed runtime cannot recreate or serve wiki content.
5. Delete inventoried wiki files, projections, records, and registrations; apply
   dedicated schema retirement.
6. Reconcile stale code-index entries belonging to removed artifacts.
7. Record completion receipts and verify a fresh session receives no wiki injection.

Validate path containment and ownership before filesystem deletion. Remove symlink
entries without traversing targets. Preserve original sources outside wiki storage.
Use exact datastore targets; refuse blanket database, collection, or graph resets.
Failures record partial results and permit bounded retries. Recovery uses the
isolated backup and recorded baseline.

**Acceptance:** All inventoried legacy wiki content and derived state are gone;
remaining services cannot recreate it; unrelated files and platform state survive;
recovery has been demonstrated.

## 5. Verification and handoff

Use focused tests and isolated services to verify canonical sessions still
summarize, revise, hand off, and archive without wiki mirrors; diagnostic redaction
and hub chat-attachment forwarding retain behavior; startup, restart, installation,
project purge, and gcode indexing/search/graphs work without wiki package or binary;
MCP discovery, HTTP routing, UI navigation, and settings expose no legacy wiki.

Verify purge repeated and interrupted execution, unavailable backends, symlinks,
and out-of-scope paths. Verify unrelated datastore records and original files
remain intact, fresh sessions/restarted services neither inject nor regenerate wiki,
and task/plan registry matches retirement disposition.

Run targeted Python, Rust, frontend, schema-contract, lint, and type checks. Do not
run full pytest. Before formal Full-plan validation, fix the validator false
positive for explicit whole-file deletion of large files. Regression coverage must
distinguish complete deletion from edits still subject to size safeguards.

Before clearing context, persist a handoff containing epic/worktree identity, task
disposition, preservation boundaries, and outstanding validation. Replacement wiki
product outline remains input to separate planning after retirement.

## Execution checkpoint

Initial task #21772 owns archival and backlog disposition. No implementation or
live purge has occurred. The active session-summary epic #21769 owns canonical
summary/handoff changes on a separate review branch; coordinate shared call sites
and schema numbering before landing. The supplied plan is preserved here before
target-inventory refinement and formal validation.
