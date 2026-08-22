Local-import heritage repair

▎ Execution (user decision): direct tasks in this session — one small standalone epic
▎ (not under #17680) plus one leaf per ### N.M section (1.1, 1.2, 2.1, 3.0, 3.1,
▎ 4.1), created with gobby-tasks create_task, implemented, validated, and closed
▎ here with linked commits. No adversary-review rounds. The body is also saved as
▎ .gobby/plans/local-import-heritage-repair.md for the record.
▎
▎ Landing: this work merges first, ahead of the four in-flight goal worktrees
▎ (wt-task-17680-m3, wt-task-20488-m2, wt-task-20539-m1, wt-task-20255-m4).
▎ Work happens in a linked worktree wt-task-<epic-ref>-m0 off local 0.5.0
▎ (EnterWorktree from this session), never in the main checkout (another session
▎ holds uncommitted ghook/agy files there). Rationale: #17680's class-hierarchy view
▎ QA needs these heritage edges to exist; the only file shared with #17680 P3 is one
▎ bullet in code-index/SKILL.md (its 3.5 rewrites the file); no overlap with
▎ #20488/#20539 (Python) or #20255 (new crates; this plan adds no deps, so no
▎ Cargo.lock churn). When done: hand to QA, do not merge from this session.
▎
▎ Machine-global sequencing: ~/.gobby/bin/gcode and the index rows are shared by
▎ every checkout on this machine. Rebuilding/installing gcode from the worktree is
▎ fine for local verification (schema-identical superset of 0.5.0), but the
▎ targeted owner reparse in E1 step 4 is run once after the merge to 0.5.0 and
▎ reinstall, so files later edited under the other goals' binaries do not regain
▎ old-resolver carriers.

Overview

kind: framing

gcode repair reports local_import_inheritance: pending=52, resolved=0 on this
project and the number never moves. Every one of the 52 code_inheritance rows is
stranded by a concrete defect, not by a provider that has yet to be indexed:

┌──────┬────────────────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────┐
│ rows │                               shape                                │                              cause                              │
├──────┼────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ 26   │ impl fmt::Display for X                                            │ A — same-file method fmt shadows the use std::fmt; module alias │
├──────┼────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ 7    │ impl From<..> for gobby_wiki::X inside crates/gwiki                │ B — workspace self-crate name never loaded                      │
├──────┼────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ 1    │ impl search::bm25::Trait for X after use crate::search;            │ C — local module alias ignored by the qualified-path resolver   │
├──────┼────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ 1    │ use super::super::sink::CodeFactSink                               │ D — only one super level is stripped                            │
├──────┼────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ 17   │ base re-exported from mod.rs / lib.rs / __init__.py / index/api.rs │ E — name lookup never leaves the candidate file                 │
└──────┴────────────────────────────────────────────────────────────────────┴─────────────────────────────────────────────────────────────────┘

Because heritage_endpoint maps CallTargetKind::LocalImport => None
(crates/gcode/src/graph/code_graph/write/inheritance.rs:215), these rows are
dropped from the FalkorDB projection entirely — WebSocketServer INHERITS VoiceMixin,
PostgresWikiStore IMPLEMENTS WikiIndexStore, every IMPLEMENTS ConfigSource, etc.
are invisible to class-hierarchy walks and blast radius. The calls projection already
degrades LocalImport to Unresolved (write/mutation.rs:773-790); inheritance is
the odd one out.

Relationship to #17680 (class-hierarchy graph): not the open P3 (view surface,
#20514). A–D are pre-existing Rust import-resolution defects that P1 (1.2/1.3,
closed) inherited; E is the by-design gap P1 documented ("LocalImport rows stay
retryable until promotion"). This plan fixes the resolver, brings the inheritance
projection to parity with calls, adds a bounded re-export fallback to promotion, and
reparses the owner files of the existing bad rows.

Size: four code leaves plus one docs leaf; ~8 production files in
crates/gcode/src/index/import_resolution/, db/, graph/code_graph/write/, all
with existing unit/DB-test harnesses. Medium-light.

Constraints

kind: framing

- Keep the P1 carrier contract from .gobby/plans/class-hierarchy-graph.md: a
  promotion miss stays LocalImport with its *_external_module candidate carrier.
  Never demote a miss to Unresolved in PostgreSQL. Visibility is fixed in the
  projection, not by rewriting rows.
- Keep the existing Rust rule (project memory): a bare leading segment that is not
  the self crate, std/core/alloc/proc_macro/test, or a Cargo.toml external
  crate resolves relative to the current module. A–D refine which inputs reach that
  fallback; they do not change the fallback.
- One resolver. Extend db::resolve_local_callee_symbol_id; do not add a second
  promotion resolver, a project-wide type index, a provenance table, or a new
  visibility type.
- No indexer-version bump. Existing rows with wrong carriers are rewritten with a
  targeted gcode index --full --files <owners> (E1), which re-parses only those
  files and replaces their (file, content_hash) inheritance rows via the existing
  content-version delete/insert (index/api.rs:604-616). Rows are shared facts keyed
  by content hash, so one machine's reparse fixes them for every machine.
- crates/gcode/src/db/queries.rs is at 906 lines; the local-callee resolver moves to
  a sibling module before growing (3.0). Every touched production .rs file stays
  under 1,000 lines.
- Rust edits load the rust skill; install rebuilt gcode via a new inode
  (cp to dotfile, mv -f over ~/.gobby/bin/gcode).
- Non-goals: cross-crate in-repo resolution (gobby_core::config::ConfigSource from
  gwiki stays External); persisting imported names in code_imports; multi-hop
  or fact-based re-export chasing; any change to code_calls miss semantics;
  view-surface work (#20514); extern crate; <T as Trait>::X (already skipped by
  strip_type_args).

P1: Rust qualified-path resolution

kind: framing

Goal: use-bound module aliases, workspace self-crate names, and multi-level
super paths resolve to the right candidate files, so new index runs stop writing
impossible carriers.

1.1 Resolve qualified paths through aliases, multi-level super, and top-level-only shadowing [category: code]

kind: deliverable

Targets:
- crates/gcode/src/index/import_resolution/parser/mod.rs::resolve_external_callee
- crates/gcode/src/index/import_resolution/parser/mod.rs::resolve_rust_local_qualified_callee
- crates/gcode/src/index/import_resolution/parser/go_rust.rs::parse_rust_import_statement
- crates/gcode/src/index/import_resolution/parser/go_rust.rs::register_rust_path_import
- crates/gcode/src/index/import_resolution/context/bindings.rs::ImportBindings
- crates/gcode/src/index/import_resolution/rust_local.rs::rust_module_for_segments
- crates/gcode/src/index/import_resolution/rust_local.rs::rust_super_module
- crates/gcode/src/index/parser/calls.rs::materialize_call
- crates/gcode/src/index/parser/tests/go_rust_java_csharp.rs::* — scope-reason: add the impl-Display, local-module-alias, use … as, super::super, Self::, ::std:: and pub use regressions next to
  classifies_external_rust_use_alias_and_path_calls
- crates/gcode/src/index/import_resolution/tests/import_statement_parsing.rs::* — scope-reason: assert the new alias map and pub use seeding next to
  rust_grouped_imports_register_named_bare_bindings

Defect A — nested symbols shadow qualifier roots. resolve_external_callee
(parser/mod.rs:180) returns None when any same-file symbol's name == root_alias.
For

use std::fmt;
impl fmt::Display for CliError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { .. }
}

the method symbol CliError::fmt (parent_symbol_id: Some(..)) shadows the fmt
module alias, the external lookup is skipped, and the qualified fallback invents
<dir>/fmt.rs. A nested member is only reachable through its container in every
indexed language and can never be a qualifier root, so restrict the check at :180:

if symbols
    .iter()
    .any(|symbol| symbol.parent_symbol_id.is_none() && symbol.name == root_alias)
{
    return None;
}

Leave the bare-call check at :153 unchanged. Do not touch
shadowing::external_call_is_shadowed (textual local-binding check; not the trigger).

Defect C — local module aliases. resolve_rust_local_qualified_callee
(parser/mod.rs:441) never sees ImportBindings, so use crate::search; followed by
impl search::bm25::Bm25SearchBackend for StoreBm25Backend falls to the bare-root
fallback (support/search/search/bm25.rs). Record the raw use path per local alias
and rebase the qualifier onto it:

- Add rust_local_modules: HashMap<String, String> (alias → normalized use path,
  e.g. "search" → "crate::search", "y" → "crate::x" for use crate::x as y;) to
  ImportBindings. Populate it in register_rust_path_import at the local-insert
  branch (go_rust.rs:175-185) where path and local_alias are both in scope. {self}
  groups already collapse to the prefix (helpers.rs:152-157), so
  use crate::search::{self, bm25} is covered.
- Thread import_bindings: &ImportBindings into resolve_rust_local_qualified_callee
  from materialize_call (calls.rs:128; ctx.import_bindings is already in scope
  there). Before delegating, split the qualifier on ::; if its first segment is a key
  in rust_local_modules and no top-level same-file symbol has that name (same rule
  as A), rewrite the qualifier to <stored path>::<rest> and call the existing
  rust_qualified_candidate with the rewritten path. crate/self/super/self-crate
  handling then applies for free. No change to LocalCallBinding or RustLocalTarget.

Defect D — super::super. rust_super_module (rust_local.rs:113) pops one level
and appends the remaining segments verbatim, so super::super::sink from
index::indexer::tests::facts becomes index::indexer::tests::super::sink. Consume
every leading super:

fn rust_super_module(current_module: &str, rest: &[&str]) -> String {
    let mut base: Vec<&str> = current_module.split("::").filter(|p| !p.is_empty()).collect();
    base.pop();
    let mut rest = rest;
    while let Some((&"super", tail)) = rest.split_first() {
        base.pop();
        rest = tail;
    }
    base.extend(rest.iter().copied());
    base.join("::")
}

Adjacent guards (same functions, cheap):
- rust_module_for_segments: add "Self" => None so Self::new() never writes a
  <dir>/Self.rs carrier.
- parse_rust_import_statement (go_rust.rs:103) only strips use ; a
  pub use … / pub(crate) use … declaration seeds no bindings at all. Strip a
  leading visibility (pub, pub(crate), pub(super), pub(in …)) before
  strip_prefix("use ") so pub use crate::x; then x::Y resolves in the same file.

Tests. Unit tests in rust_local.rs::tests for multi-level super (from a .rs file
and from a mod.rs context) and for Self. Parser-level tests in
go_rust_java_csharp.rs via parse_rust (parser/tests/common.rs:58) with a
[package] name = "app" manifest:
- impl fmt::Display snippet above → inheritance row target_name == "Display",
  target_kind == External, target_external_module == Some("std::fmt"); no row
  carries a candidate ending in fmt.rs.
- use crate::search; + impl search::bm25::Trait for S → LocalImport candidates
  src/search/bm25.rs, src/search/bm25/mod.rs.
- use crate::x as y; + y::Z::run() → candidates src/x.rs, src/x/mod.rs.
- use super::super::sink::CodeFactSink; from src/a/b/c.rs (use parse_source
  directly) → candidates src/sink.rs, src/sink/mod.rs.
- ::std::fmt::Display → External std::fmt.
- pub use crate::x; + x::Y() → LocalImport candidates for x.
  import_statement_parsing.rs: use crate::search; registers
  rust_local_modules["search"] == "crate::search".

Acceptance:

- 1.1.1 - A same-file nested symbol named like a use-bound module alias no longer shadows it; impl fmt::Display rows persist as External/std::fmt. test:
  crates/gcode/src/index/parser/tests/go_rust_java_csharp.rs::impl_display_method_does_not_shadow_fmt_module_alias.
- 1.1.2 - A qualifier rooted at a local module alias (plain or as-renamed) resolves under that module. test:
  crates/gcode/src/index/parser/tests/go_rust_java_csharp.rs::qualified_path_through_local_module_alias_resolves_under_alias_module.
- 1.1.3 - Every leading super pops one module level. test: crates/gcode/src/index/import_resolution/rust_local.rs::import_target_resolves_multi_level_super.
- 1.1.4 - Self:: qualifiers and leading-:: std paths never produce local candidates. test:
  crates/gcode/src/index/parser/tests/go_rust_java_csharp.rs::self_and_leading_colon_paths_do_not_invent_local_modules.
- 1.1.5 - pub use declarations seed import bindings. test: crates/gcode/src/index/import_resolution/tests/import_statement_parsing.rs::rust_pub_use_registers_local_bindings.
- 1.1.6 - ImportBindings.rust_local_modules maps every local use alias to its path. symbol: ImportBindings.

1.2 Load workspace member crate names [category: code] (depends: 1.1)

kind: deliverable

Targets:
- crates/gcode/src/index/import_resolution/context/package_metadata.rs::load_rust_self_crate_name
- crates/gcode/src/index/import_resolution/context/package_metadata.rs::rust_manifest_paths
- crates/gcode/src/index/import_resolution/context.rs::ImportResolutionContext
- crates/gcode/src/index/import_resolution/context.rs::ImportResolutionContext.rust_import_candidate
- crates/gcode/src/index/import_resolution/context.rs::ImportResolutionContext.rust_qualified_candidate
- crates/gcode/src/index/import_resolution/context.rs::build_import_resolution_context_with_overrides
- crates/gcode/src/index/import_resolution/predicates.rs::is_external_rust_root
- crates/gcode/src/index/import_resolution/predicates.rs::rust_external_roots
- crates/gcode/src/index/import_resolution/parser/mod.rs::seed_import_bindings
- crates/gcode/src/index/parser.rs::* — scope-reason: pass rel_path into seed_import_bindings at the single production call site
- crates/gcode/src/index/import_resolution/rust_local.rs::rust_module_context_for_rel_path
- crates/gcode/src/index/import_resolution/tests/common.rs::* — scope-reason: retarget the rust_self_crate_name re-export to the map
- crates/gcode/src/index/import_resolution/tests/context_loading.rs::* — scope-reason: add the workspace-member test next to normalizes_rust_package_name_from_cargo_toml and fix the
  seed_import_bindings call
- crates/gcode/src/index/parser/tests/go_rust_java_csharp.rs::* — scope-reason: extend classifies_rust_workspace_member_dependencies with a file under crates/app/src/

Defect B. load_rust_self_crate_name (package_metadata.rs:173) reads only
<root>/Cargo.toml; a workspace root has no [package], so rust_self_crate_name
is None and gobby_wiki::AiDepth inside crates/gwiki/src/cli/code.rs is treated as
a bare module → cli/code/gobby_wiki.rs.

Replace the scalar with a map keyed by project-relative source root:

/// `crates/gwiki/src` -> `gobby_wiki`; a single-crate repo maps `src` -> name.
pub(super) rust_self_crate_names: HashMap<String, String>,

Build it in package_metadata.rs from the manifests rust_manifest_paths already
enumerates (root + literal/glob members): for each manifest with [package].name,
key = <manifest dir relative to root_path>/src (root manifest → src), value =
normalize_rust_crate_name(name). The key matches the rposition("src") rule in
rust_module_context_for_rel_path (rust_local.rs:84); expose that context's
source_root (or add ImportResolutionContext::rust_self_crate_name_for(rel_path) -> Option<&str>) and pass the looked-up name where rust_self_crate_name.as_deref()
is passed today. Call sites that change (all have rel_path in scope or get it):
context.rs:158,178 (rust_import_candidate, rust_qualified_candidate),
predicates.rs:247 (is_external_rust_root; sole caller go_rust.rs:193),
predicates.rs:73 (rust_external_roots; sole caller seed_import_bindings
parser/mod.rs:91-107, whose production caller index/parser.rs:472 has rel_path
at :466), tests/context_loading.rs:111,302, tests/common.rs:10.

A HashSet of all workspace names is not acceptable: gwiki depends on
gobby-core/gobby-code (crates/gwiki/Cargo.toml:29-30), so a set would make
gobby_core::X from gwiki a crate-root path into crates/gwiki/src/lib.rs. Sibling
crates stay External via the dependency set.

Keep single-crate behaviour: root [package] maps src → name;
normalizes_rust_package_name_from_cargo_toml keeps passing (asserting on the map).
The parse_rust harness pins src/main.rs; the workspace test must call
parse_source("crates/app/src/main.rs", …) directly.

Acceptance:

- 1.2.1 - Workspace members each resolve their own package name; app::service::run from a file under crates/app/src maps to crates/app/src/service.rs / service/mod.rs, and serde_json::from_str
  stays external. test: crates/gcode/src/index/parser/tests/go_rust_java_csharp.rs::classifies_rust_workspace_member_dependencies.
- 1.2.2 - The loader maps <member>/src → normalized package name for root, literal and glob members. test:
  crates/gcode/src/index/import_resolution/tests/context_loading.rs::loads_rust_workspace_member_self_crate_names.
- 1.2.3 - Single-crate repos keep src → [package].name. test: crates/gcode/src/index/import_resolution/tests/context_loading.rs::normalizes_rust_package_name_from_cargo_toml.

P2: Pending rows in the projection

kind: framing

Goal: pending LocalImport heritage rows appear in FalkorDB as Unresolved
endpoints while PostgreSQL keeps the retryable carrier — parity with the calls path.

2.1 Project LocalImport heritage endpoints as Unresolved [category: code]

kind: deliverable

Targets:
- crates/gcode/src/graph/code_graph/write/inheritance.rs::heritage_endpoint
- crates/gcode/src/graph/code_graph/tests.rs::heritage_local_import_is_not_projected
- crates/gcode/src/models.rs::CallTargetKind

heritage_endpoint (write/inheritance.rs:207-240) returns None for
CallTargetKind::LocalImport, and partition_inheritance_graph_items (:164-205)
skips the whole edge on None; sync_plan.rs does not pre-filter. Map it exactly
like Unresolved:

CallTargetKind::LocalImport | CallTargetKind::Unresolved => {
    if name.is_empty() { return None; }
    Some((HeritageEndpoint::Unresolved, make_unresolved_callee_id(project_id, name), None))
}

The PostgreSQL row is untouched. Promotion already dirties the owner in the same
transaction (local_imports.rs:228-236), re-sync mints a new token (write.rs:78)
and delete_stale_file_graph_queries drops the old Unresolved edge by token mismatch
(write/deletion.rs:131-177); the UnresolvedCallee node id is shared with the calls
side, so no double-count. A dangling node lingers until cleanup-orphans, same as
unresolved calls today.

Replace heritage_local_import_is_not_projected (tests.rs:1085-1105) with
local_import_heritage_endpoints_project_as_unresolved: a Symbol→LocalImport row
emits an INHERITS query whose rows param contains
make_unresolved_callee_id(project, "Helper"); a LocalImport→LocalImport row emits
the Unresolved→Unresolved variant. Update the CallTargetKind::LocalImport doc
comment in models.rs to state that pending inheritance rows project as Unresolved.

Acceptance:

- 2.1.1 - LocalImport sources and targets project as Unresolved endpoints with make_unresolved_callee_id; both-sides-LocalImport rows project as Unresolved→Unresolved. test:
  crates/gcode/src/graph/code_graph/tests.rs::local_import_heritage_endpoints_project_as_unresolved.
- 2.1.2 - CallTargetKind::LocalImport documentation states the projection behaviour. symbol: CallTargetKind.

P3: Re-export fallback in promotion

kind: framing

Goal: a pending row whose candidate is a module-root file promotes when the
symbol is defined, uniquely and at top level, anywhere under that module's directory.

3.0 Move the local-callee resolver into db/local_callee.rs [category: refactor]

kind: deliverable

Targets:
- crates/gcode/src/db/queries.rs::resolve_local_callee_symbol_id
- crates/gcode/src/db/queries.rs::resolve_default_import_symbol_id
- crates/gcode/src/db/queries.rs::LocalCalleeCandidate
- crates/gcode/src/db/queries.rs::select_local_callee_candidate_id
- crates/gcode/src/db/queries.rs::select_default_import_candidate_id
- crates/gcode/src/db/queries.rs::unique_id
- crates/gcode/src/db/local_callee.rs
- crates/gcode/src/db/mod.rs::* — scope-reason: add mod local_callee; pub use local_callee::*;
- crates/gcode/security/managed_postgres_privileges.json::* — scope-reason: add the db/local_callee.rs source_inventory entry and lower db/queries.rs query count

Behaviour-free move. queries.rs is 906 lines; the cluster queries.rs:606-757 plus
its unit tests (:838-893, ~210 lines) moves verbatim to a new
crates/gcode/src/db/local_callee.rs. db/mod.rs already does
pub use queries::*; pub use resolution::*; — add the same for local_callee, so
callers (index/indexer/local_imports.rs:73,196,210, graph/tests/standalone_db.rs:64)
do not change. tests/code_index/test_gcode_privilege_manifest.py rglobs every
non-test .rs, so add {"path": "crates/gcode/src/db/local_callee.rs", "calls": {"query": 2}, "classification": "project-write"} (exact count from the moved code)
and reduce queries.rs query from 11 accordingly. Commit separately from 3.1.

Acceptance:

- 3.0.1 - The resolver cluster lives in crates/gcode/src/db/local_callee.rs with identical behaviour and its unit tests. file: crates/gcode/src/db/local_callee.rs.
- 3.0.2 - Privilege manifest matches the per-file call inventory. test: tests/code_index/test_gcode_privilege_manifest.py::test_manifest_covers_every_rust_database_call_at_head.

3.1 Add the module-root subtree fallback [category: code] (depends: 3.0)

kind: deliverable

Targets:
- crates/gcode/src/db/local_callee.rs::resolve_local_callee_symbol_id
- crates/gcode/src/index/indexer/tests/facts.rs::* — scope-reason: add the re-export promotion fixtures next to inheritance_local_import_miss_stays_retryable
- crates/gcode/security/managed_postgres_privileges.json::* — scope-reason: bump the db/local_callee.rs query count for the fallback query

Defect E. resolve_local_callee_symbol_id searches code_symbols by name only
inside the candidate files. 17 rows name a base that the candidate re-exports from a
descendant (config/mod.rs → config/resolve.rs, voice/__init__.py →
voice/mixin.py, gobby/ai/__init__.py → ai/text_generation.py →
ai/_text_generation_adapters.py, lib.rs → error.rs). code_imports persists
module strings only and Rust pub use seeds nothing, so fact-based chasing is not
available, and P1 forbids a project-wide type index.

Bounded fallback inside resolve_local_callee_symbol_id (calls benefit too):

1. Run the existing exact-file query. If it returns any rows, tier-select as
   today and return (an ambiguous exact hit must not widen).
2. Otherwise collect module-root candidates: basename is mod.rs, lib.rs,
   main.rs, __init__.py/__init__.pyi, or index.{js,jsx,ts,tsx,mjs,cjs}. Map
   each to its directory (lib.rs/main.rs → the crate src dir), dedupe
   (gobby/ai/ and src/gobby/ai/ both appear in Python candidate sets; a
   non-existent dir matches nothing because of the active-state join). If none, return
   None.
3. Run the same query with s.file_path LIKE $prefix ESCAPE '\' per prefix (escape
   \, %, _ in the dir), joined to the active code_indexed_file_states row
   exactly like step 1, restricted to top-level rows (parent_symbol_id IS NULL,
   kind in function/class/type) — a re-export can only name a top-level item,
   and skipping methods avoids new/from collisions on the calls path. Feed the
   union through select_local_callee_candidate_id; the unique guard keeps an
   ambiguous subtree pending. Factor the shared SQL into a private helper that takes
   the path predicate.

A plain-file candidate (index/api.rs) does not widen, so IndexProgressSink stays
pending and is covered by 2.1.

Tests in facts.rs::serial_db using the existing fixtures (seeded_project,
write_derived_pending, write_base_symbol, inheritance_row, count_inheritance),
gated with #[cfg_attr(not(gcode_postgres_tests), ignore)] +
#[serial_test::serial(serial_db)]:
- pkg/__init__.py candidate, Base defined in pkg/impl.py → promotes to that id,
  one row.
- Rust store/mod.rs candidate, Store trait (kind type) in store/types.rs →
  promotes.
- Base defined in both pkg/a.py and pkg/b.py → stays local_import, carrier intact.
- Candidate pkg/api.py (not a module root), Base in pkg/impl.py → stays pending.
- A method named Base under the subtree does not promote (top-level filter).

Acceptance:

- 3.1.1 - A base re-exported from a module-root candidate promotes to the defining top-level symbol's id with no duplicate row. test:
  crates/gcode/src/index/indexer/tests/facts.rs::inheritance_reexported_base_promotes_via_module_root_subtree.
- 3.1.2 - An ambiguous subtree leaves the row LocalImport with its carrier. test: crates/gcode/src/index/indexer/tests/facts.rs::inheritance_ambiguous_subtree_stays_retryable.
- 3.1.3 - Non-module-root candidates and nested same-name members never widen. test: crates/gcode/src/index/indexer/tests/facts.rs::inheritance_plain_file_candidate_does_not_widen.
- 3.1.4 - Privilege manifest matches the new query count. test: tests/code_index/test_gcode_privilege_manifest.py::test_manifest_covers_every_rust_database_call_at_head.

P4: Docs

kind: framing

Goal: operator docs describe pending-row projection, the re-export fallback, and
the targeted reparse path.

4.1 Document repair behaviour and targeted reparse [category: docs] (depends: 2.1, 3.1)

kind: deliverable

Targets:
- docs/guides/code-index.md
- src/gobby/install/shared/skills/code-index/SKILL.md

In docs/guides/code-index.md:224-231 (the gcode repair paragraph) add: pending
LocalImport inheritance rows project as UnresolvedCallee endpoints until promoted;
promotion searches module-root candidates' subtrees for a unique top-level definition;
rows stranded by a resolver change are rewritten with
gcode index --full --files <owner paths> (the hash shortcut is skipped under
--full, and the file's (file, content_hash) rows are replaced). In the code-index
skill's gcode repair bullet add the same one-line note. No contract change: the
repair JSON shape is unchanged and docs/contracts/shared-graph-schema.md already
lists UnresolvedCallee as a heritage endpoint.

Acceptance:

- 4.1.1 - The code-index guide describes pending-row projection, the subtree fallback, and the targeted reparse command. file: docs/guides/code-index.md.
- 4.1.2 - The code-index skill's repair bullet matches. file: src/gobby/install/shared/skills/code-index/SKILL.md.

E1 End-to-end verification

kind: verification

1. cargo fmt --check; cargo clippy --workspace --all-targets -- -D warnings.

1. cargo fmt --check; cargo clippy --workspace --all-targets -- -D warnings.
2. Focused tests: cargo test -p gobby-code import_resolution;
   cargo test -p gobby-code parser::tests::go_rust_java_csharp;
   cargo test -p gobby-code graph::code_graph::tests; with
   GCODE_POSTGRES_TEST_DATABASE_URL set to a _test database:
   cargo test -p gobby-code index::indexer::tests::facts and db::local_callee;
   cargo test -p gobby-code --test contract;
   GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_gcode_privilege_manifest.py;
   uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new.
3. Build release gcode; install via new inode.
4. Targeted reparse of the owners of today's bad rows:
   psql "$DB" -At -c "SELECT DISTINCT ci.file_path FROM code_inheritance ci JOIN code_indexed_file_states s USING (project_id, file_path, content_hash) WHERE ci.project_id='<id>' AND
   s.machine_id='<machine>' AND (ci.source_kind='local_import' OR ci.target_kind='local_import')"
   → gcode index --full --files <those paths>; then gcode repair --format json.
   Expect local_import_inheritance.pending ≤ 1 (IndexProgressSink) and the
   26+7+1+1 resolver rows gone from PostgreSQL (rewritten as external/symbol),
   the 16 re-export rows promoted (resolved ≥ 16 on the first repair run).
5. SQL spot checks: no *_external_module ending in fmt.rs, gobby_wiki.rs, or
   containing /super/; ConfigSource, WikiIndexStore, VoiceMixin, HandlerMixin,
   AgyCLITextGenerateAdapter, CitationResolver, WikiError rows are symbol.
6. Graph: GRAPH.QUERY for (:CodeSymbol {name:'WebSocketServer'})-[:INHERITS]->()
   returns VoiceMixin and HandlerMixin CodeSymbol nodes;
   (:CodeSymbol {name:'StderrIndexProgress'})-[:IMPLEMENTS]->(:UnresolvedCallee)
   exists for IndexProgressSink. gcode repair steady-state stays ~0.5 s.
