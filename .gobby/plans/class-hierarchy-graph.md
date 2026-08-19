Plan artifact: `.gobby/plans/class-hierarchy-graph.md`

# Class Hierarchy Graph

> **Plan ID:** class-hierarchy-graph
> **Root task:** #17680 (currently `task_type: feature`; convert to epic at human handoff, then expand children under it)

## Overview
`kind: framing`

Add the missing Class Hierarchy Graph to gcode: extract explicit inheritance edges, persist them in PostgreSQL, project `INHERITS` / `EXTENDS` / `IMPLEMENTS` into FalkorDB, and expose a scoped `gcode graph view` surface (`fcg`, `mcg`, `class-hierarchy`) plus `gcode callees`. This completes the DUALVIEW adoption from #17613 on top of the #18786 seed-scoped query API. Symbol summaries, tagged comments, and the rest of the #17678 code-fact contract stay on #17678.

## Constraints
`kind: framing`

Decision Record:

- Full Plan-Coverage artifact; expand implementation children under existing `#17680`. Live `#17680` is still `task_type: feature`. Convert it to an epic (or bind another eligible open epic) only at explicit human handoff / expansion — not during review. Section 3.5 owns the post-expansion description update; it does not convert the type.
- Ship the full graph-view product: `gcode graph view --view=fcg|mcg|class-hierarchy` and `gcode callees`.
- MCG communities are the existing weighted Leiden implementation in `crates/gcore/src/graph_analytics/leiden.rs` via `graph_analytics::analyze` (`DEFAULT_GAMMA = 1.0`). Do not add a second community algorithm.
- Extract explicit heritage only. Class-language extends/implements. Rust: `impl Trait for Type` → `IMPLEMENTS`, `trait Foo: Bar` → `EXTENDS`. Go: struct embedding and interface embedding only. No implicit Go method-set matching. No rustc or go/types.
- Class-hierarchy walks the complete ancestor/descendant DAG from the seed, hop-capped at `--depth`. `--depth` is an optional clap value in `1..=16` (`MAX_SYMBOL_PATH_DEPTH`); it has **no** single materialized default. After `ViewKind` resolution the effective default is **8** for `class-hierarchy` and **1** for `fcg` / `mcg`. CHG uses no row `LIMIT` and does not reuse `scoped_edges` as the complete-DAG reader. FCG and MCG keep independent incoming/outgoing limits and #18786 truncation metadata (`incoming_truncated` / `outgoing_truncated`). Those flags are structured edge-limit or hop-cut metadata, not output clipping. `--incoming-limit` / `--outgoing-limit` are FCG/MCG-only; `class-hierarchy` plus either flag is a clap / user error.
- Stored heritage orientation is derived → base. From the seed, **outgoing** edges are ancestors and **incoming** edges are descendants. Hop-cut flags follow that mapping. CHG keeps two independent directed frontiers (outgoing-only ancestors, incoming-only descendants) so a hop cannot switch direction and admit siblings or cousins.
- Mermaid node IDs are opaque injective tokens (`n0`, `n1`, …) assigned from the sorted **typed** canonical key set (`symbol:<uuid>`, `file:<path>`, `module:<name>`, `external:<id>`, `unresolved:<id>`). Escaped labels stay separate and keep the raw path, name, or uuid. Do not sanitize a symbol id, path, or module name into the identifier, and do not use a raw path or module name as the canonical key — a `CodeFile.path` can equal a `CodeModule.name`.
- A token budget on **synthesis** is allowed: instruct a model to produce under N tokens ("summarize this in under 10K tokens"). A token budget on **output** is forbidden: never `text.left(10000)`, `trim_results`, or Graphify `query --budget` string cutting of already-rendered bytes. `gcode graph view` and `gcode callees` are deterministic dumps, not synthesis — emit the complete JSON object and a complete Mermaid block, or error. The Mermaid block must pass `gobby_core::vault::mermaid::is_valid_mermaid`. Do not add an output-clip `--token-budget` to `graph view`. `gcode callees` mirrors `gcode callers` pagination (`limit`/`offset`) and does not inherit `usages`/`search`/`blast-radius` row trim. A later summarized view may take a synthesis budget; that is a generation instruction, not a post-hoc slice.
- `#17679` already fixed `gcode graph report` by bounding that query. Unbounded walks remain out of contract.

Non-goals:

- Program Dependence Graph.
- Implicit Go interface satisfaction.
- Image / PNG visual externalization.
- Symbol-summary persistence, tagged-comment spans, and the rest of the #17678 code-fact widening.
- Forced full-project reindex. New edges appear when a file is next indexed; `gcode graph rebuild` projects whatever PostgreSQL already has.
- The closed `#17613` research before/after exploration-step measurement. Shipping the views is the DUALVIEW adoption; that measurement is not an `#17680` acceptance bar and is not preserved on `#17678`.
- A second C#/Java declaration-index scan for explicit consumer-only indexing. Heritage uses the same `extract_calls` / `materialize_call` tiers; do not invent a project-wide type index that calls do not run.
- Vector/Qdrant completion CAS or `mark_vectors_synced` changes. This plan dirties graph state only.
- A new public or private `(project_id, file_path, content_hash)` visibility type. Reuse the existing visibility helpers; do not invent a second owner-identity store.

File-size ceiling: `crates/gcode/src/index/languages.rs` is 814 lines. Inheritance query strings go in a new module, not inlined into that file. `parser.rs` (478) gets a sibling `parser/inheritance.rs`, matching `parser/calls.rs`. The view surface is `crates/gcode/src/commands/graph/view/` (`mod.rs`, `render.rs`, `fcg.rs`, `mcg.rs`, `class_hierarchy.rs`), not one flat view module. Every touched production file stays under 1,000 lines. Leave `crates/gcode/src/commands/graph/reads.rs` (912) focused on callers/callees.

Edge direction: derived → base. `(Derived)-[:EXTENDS|IMPLEMENTS|INHERITS]->(Base)`. This matches the #18786 Cypher already in `match_clause`. Walk labels: outgoing = ancestors, incoming = descendants.

LocalImport heritage rows stay retryable (`LocalImport` plus candidate files) until promotion or the derived file is reindexed. Do not demote a miss to `Unresolved` in a way that drops the import carrier. After promotion, invalidation matches calls: reindex of the derived file rewrites the row; do not add a second provenance store.

Heritage MERGE writers set `sync_token` the same way CALLS do. Stale-edge delete is token-only.

After expansion, update `#17680` description and `validation_criteria` to name fcg, mcg, callees, Leiden-on-mcg, and complete CHG — the written bar today is CHG-only. Section 3.5 owns that update.

## P1: Inheritance facts
`kind: framing`

**Goal**: The indexer emits typed inheritance relations and PostgreSQL stores them the same way it stores `code_calls`.

### 1.1 Add inheritance model, schema, and privileges [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/models.rs::*` — scope-reason: add HeritageKind, InheritanceRelation, ParseResult.inheritance, independent LocalImport helpers, and the CallTargetKind miss-path comment
- `crates/gcode/src/schema.rs::*` — scope-reason: add code_inheritance to TABLE_CONTRACTS and REQUIRED_TABLES and keep validate_runtime_schema in lockstep
- `crates/gcode/src/index/parser.rs::*` — scope-reason: declare the inheritance module sibling and pass an empty inheritance vec from parse_file_with_semantic until 1.2 extracts rows
- `crates/gcode/src/index/parser/tests/common.rs::*` — scope-reason: add inheritance: Vec::new() to every ParseResult helper so 1.1 compiles before extract_inheritance exists
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: register the next unused code_inheritance migration and refresh baseline checksum constants
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: register and verify code_inheritance on the standalone GcoreCodeIndex adoption path without breaking pre-inheritance schemas
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: add code_inheritance catalog objects including pkey, unique, and content-version FK
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: pin Python expected gcore identity to the new hop
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: bump GOLDEN_BASELINE_CHECKSUM, GOLDEN_LATEST_CHECKSUM, GOLDEN_ASSETS_ROOT_HASH, and expected_schema_identity together
- `crates/gcore/src/grant/tests.rs::expected_schema_identity_tracks_catalog_head`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: assert latest_version tracks the new catalog head
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: regenerate schema_identity after the hop
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: regenerate schema_identity after the hop
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: regenerate schema_identity after the hop
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: regenerate schema_identity after the hop
- `tests/runtime_grants/test_golden_vectors.py::*` — scope-reason: golden rewrite recipe for schema_identity / payload_checksum / signature
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: grant code_inheritance table and sequence privileges so the existing gcode project-policy applier emits gobby_gcode_project_{read,insert,update,delete}
- `crates/gcore/tests/fixtures/schema/parent_baseline.sql`
- `crates/gcore/tests/fixtures/schema/predecessor_baseline.sql`
- `crates/gcore/tests/fixtures/schema/worktree_baseline.sql`
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: assert code_inheritance is in the generated gcode project policy inventory, heritage_kind CHECK, external-schema inventory, and standalone adoption skip/apply path
- `crates/gcore/src/schema/external.rs::gcode_postgres_objects`
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: add inheritance to every ParseResult literal and cleanup deletes
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: add inheritance to every ParseResult sink fixture
- `tests/code_index/test_gcode_privilege_manifest.py::*` — scope-reason: exact managed relation-set contract must include code_inheritance next to code_calls

Add `InheritanceRelation` next to `CallRelation` in `crates/gcode/src/models.rs`. Reuse `CallTargetKind` (`Symbol`, `Unresolved`, `External`, `LocalImport`) for **both** endpoints. Targets use the four `extract_calls` tiers. Rust impl `Type` sources use those same four tiers (1.2): same-file `Symbol`, in-repo `LocalImport` until promotion, crate/stdlib `External`, otherwise `Unresolved`. Do not invent a fifth source kind. Update the `CallTargetKind::LocalImport` doc comment in the same leaf: call-graph misses still rewrite to `Unresolved` and drop the carrier after an index run; inheritance misses stay `LocalImport` with that side's `*_external_module` carrier so a later provider index can retry. The kind therefore persists past a completed index run for inheritance rows only. Add `heritage_kind` as a small enum, not a stringly type:

```rust
pub enum HeritageKind {
    Inherits,
    Extends,
    Implements,
}

impl HeritageKind {
    pub fn as_rel_type(self) -> &'static str {
        match self {
            Self::Inherits => "INHERITS",
            Self::Extends => "EXTENDS",
            Self::Implements => "IMPLEMENTS",
        }
    }
}

pub struct InheritanceRelation {
    pub source_symbol_id: Option<String>,
    pub source_name: String,
    pub source_kind: CallTargetKind,
    pub source_external_module: Option<String>,
    pub target_symbol_id: Option<String>,
    pub target_name: String,
    pub target_kind: CallTargetKind,
    pub target_external_module: Option<String>,
    pub heritage_kind: HeritageKind,
    pub file_path: String,
    pub content_hash: String,
    pub line: usize,
}

pub struct ParseResult {
    pub symbols: Vec<Symbol>,
    pub imports: Vec<ImportRelation>,
    pub calls: Vec<CallRelation>,
    pub inheritance: Vec<InheritanceRelation>,
    pub source: Vec<u8>,
}
```

PostgreSQL table `code_inheritance` mirrors `code_calls` plus `heritage_kind` and nullable source identity (same-file sources are `Symbol`; in-repo cross-file Rust `impl Trait for Type` sources stay `LocalImport` until promotion; crate/stdlib or unknown Type sources persist as `External` / `Unresolved` terminals and are not promotion candidates). Persist **independent** LocalImport carriers on both endpoints: `source_external_module` and `target_external_module`, each using the same joined-candidate encoding as `CallRelation::local_import_candidate_files` (`callee_external_module` + `LOCAL_IMPORT_CANDIDATE_SEP`). A row may be `LocalImport` on both sides at once (cross-file `impl Trait for Type` when Trait is also imported). Helpers `source_local_import_candidate_files` / `target_local_import_candidate_files` read those columns; promotion of one side clears only that side's carrier. Add the table to `baseline.sql` (new installs) and to a new hop for existing hubs. At implementation start take `max(existing embedded migration versions)+1` across the live `MIGRATIONS` registry **and** `crates/gcore/assets/schema/migrations/` filenames. Committed catalog head at this writing is version 390 (interactive credential-material retain hop). The worktree may already hold a 391 session-activity hop; do **not** reuse 387, 388, 389, 390, or any version that already exists as a file or `MIGRATIONS` entry. Do not pin a numbered hop filename in this plan. At implementation start, create the next unused hop file in the existing migrations directory using that integer prefix. Register the `EmbeddedMigration` in `MIGRATIONS` and refresh `BASELINE_CHECKSUM` / `catalog.manifest.json` through the existing gcore schema verify path (`gdaemon schema verify` / crate tests). Do not hand-edit checksums.

Identity pin recipe (same as other hops): bump `src/gobby/storage/schema_expected_identity.json` (`latest_version` / `latest_checksum` / `assets_root_hash`) from a rebuilt `gdaemon` via `scripts/generate_schema_expected_identity.py`; update `GOLDEN_LATEST_CHECKSUM`, `GOLDEN_ASSETS_ROOT_HASH`, and the no-postgres `latest_version` in `crates/gcore/src/grant/bundle.rs`; pin `crates/gcore/tests/schema_contract.rs`, `expected_schema_identity_tracks_catalog_head`, and `crates/gdaemon/tests/cli_contract.rs`; regenerate the four files under `tests/runtime_grants/golden/` per the `test_golden_vectors.py` docstring.

```sql
CREATE TABLE code_inheritance (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    source_symbol_id uuid,
    source_name text NOT NULL,
    source_kind text DEFAULT 'symbol'::text NOT NULL,
    source_external_module text DEFAULT ''::text NOT NULL,
    target_symbol_id uuid,
    target_name text NOT NULL,
    target_kind text DEFAULT 'unresolved'::text NOT NULL,
    target_external_module text DEFAULT ''::text NOT NULL,
    heritage_kind text NOT NULL,
    file_path text NOT NULL,
    content_hash text NOT NULL,
    line integer DEFAULT 0 NOT NULL
);

ALTER TABLE ONLY code_inheritance FORCE ROW LEVEL SECURITY;

ALTER TABLE code_inheritance ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME code_inheritance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE ONLY code_inheritance
    ADD CONSTRAINT code_inheritance_pkey PRIMARY KEY (id);

ALTER TABLE ONLY code_inheritance
    ADD CONSTRAINT code_inheritance_unique_target UNIQUE NULLS NOT DISTINCT (
        project_id, file_path, content_hash,
        source_symbol_id, source_name, source_kind, source_external_module,
        target_symbol_id, target_name, target_kind, target_external_module,
        heritage_kind, line
    );

ALTER TABLE ONLY code_inheritance
    ADD CONSTRAINT code_inheritance_content_fkey
    FOREIGN KEY (project_id, file_path, content_hash)
    REFERENCES code_indexed_files(project_id, file_path, content_hash)
    ON DELETE CASCADE;

CREATE INDEX idx_ci_source ON code_inheritance (project_id, source_symbol_id);
CREATE INDEX idx_ci_file ON code_inheritance (project_id, file_path);
CREATE INDEX idx_ci_target ON code_inheritance (project_id, target_kind, target_symbol_id, target_name);

ALTER TABLE ONLY code_inheritance
    ADD CONSTRAINT code_inheritance_heritage_kind_check
    CHECK (heritage_kind IN ('INHERITS', 'EXTENDS', 'IMPLEMENTS'));
```

Keep that `heritage_kind` CHECK in baseline, the hop, and the three schema fixtures. Do not leave it as prose-only. Grant the gcode capability the same `SELECT/INSERT/UPDATE/DELETE` on `code_inheritance` and `USAGE/SELECT` on `code_inheritance_id_seq` as `code_calls` in `managed_postgres_privileges.json`, including `scope_column: project_id`. That inventory is what emits `gobby_gcode_project_{read,insert,update,delete}` — do not hand-write a second `CREATE POLICY` dialect in the migration. The Python exact-set contract in `tests/code_index/test_gcode_privilege_manifest.py` must list `code_inheritance` next to `code_calls`; this leaf owns the relation/sequence parity, not later source-inventory retargeting. Add the table and columns to `TABLE_CONTRACTS` and `REQUIRED_TABLES` in `crates/gcode/src/schema.rs`. Add `code_inheritance` (table, unique, content-version FK, and the same three index shapes as `code_calls`) to `gcode_postgres_objects` so external schema provisioning creates the complete runtime table. Do not add GRANT statements there.

Standalone gcore adoption (`crates/gcore/src/schema/runner.rs`) must see the same table. Add `code_inheritance` to `GCORE_CODE_INDEX_TABLES` and `adopted_column_contracts` with the columns this leaf creates. Classification of a **pre-inheritance** schema that still has the current eight code-index tables must keep working (`GcoreCodeIndex` without requiring the new table to already exist). When the adopted schema already has `code_inheritance` (external `gcode_postgres_objects` provision), skip replaying `CREATE TABLE` and verify the adopted columns. When it does not, apply the hop / baseline statement instead of failing closed or colliding. Do not grow `runner.rs` (951) to 1,000 lines.

Copy the table into the gcore schema test fixtures that shadow `baseline.sql` (`parent_baseline.sql`, `predecessor_baseline.sql`, `worktree_baseline.sql`) so verify stays green.

Every existing `ParseResult { ... }` literal must gain `inheritance: Vec::new()` or the real rows in **this** leaf so the crate compiles before 1.2 extracts rows. That includes `parse_file_with_semantic` (empty vec until 1.2), `parser/tests/common.rs` helpers, `write_postgres_parsed_file_facts` / `write_postgres_parsed_file_facts_with_root` in `serial_db.rs`, and the `facts.rs` sink fixtures.

**Acceptance:**

- 1.1.1 - `InheritanceRelation` and `HeritageKind` exist and `ParseResult` carries `inheritance`. file: `crates/gcode/src/models.rs`.
- 1.1.2 - `code_inheritance` is in baseline, the next unused hop (`max(existing embedded versions and migration filenames)+1`, never a version that already exists), privileges, and `REQUIRED_TABLES`. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.3 - Runtime schema validation requires the new table and columns. symbol: `validate_runtime_schema`.
- 1.1.4 - `code_inheritance` is in the generated gcode project-policy inventory next to `code_calls`; same-project managed-role access succeeds and cross-project access is rejected. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_has_gcode_project_policies`.
- 1.1.5 - Serial DB and parser `ParseResult` literals compile with the new `inheritance` field. file: `crates/gcode/src/index/indexer/tests/serial_db.rs`.
- 1.1.6 - Catalog identity consumers advance with the hop: `schema_expected_identity.json`, `schema_contract.rs`, grant golden `latest_version`, and `gdaemon` cli contract. file: `src/gobby/storage/schema_expected_identity.json`.
- 1.1.7 - `code_inheritance` has `code_inheritance_pkey`, `code_inheritance_unique_target` (`UNIQUE NULLS NOT DISTINCT` including `source_external_module` and `target_external_module`), and `code_inheritance_content_fkey` `ON DELETE CASCADE` to `code_indexed_files`. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.8 - `InheritanceRelation` and `code_inheritance` structurally support independent `source_external_module` and `target_external_module` carriers (model fields, helpers, and schema columns). This leaf does not implement production insert/read or one-side-only promotion; those stay in 1.3.10. file: `crates/gcode/src/models.rs`.
- 1.1.9 - The `CallTargetKind::LocalImport` comment documents call-miss (`Unresolved`, carrier dropped) versus inheritance-miss (row stays `LocalImport` with the side's carrier). symbol: `CallTargetKind`.
- 1.1.10 - `code_inheritance_heritage_kind_check` exists and rejects every value outside `INHERITS`, `EXTENDS`, and `IMPLEMENTS`. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_heritage_kind_check_rejects_unknown`.
- 1.1.11 - After regenerating the four runtime-grant goldens, `tests/runtime_grants/test_golden_vectors.py` validates each file's canonical bytes, `payload_checksum`, and signature against the new schema identity. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
- 1.1.12 - `gcode_postgres_objects` creates `code_inheritance` with the unique constraint, content-version FK, and the three `code_calls`-shaped indexes. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_is_in_gcode_postgres_objects`.
- 1.1.13 - The managed privilege exact relation set includes `code_inheritance` next to `code_calls`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.
- 1.1.14 - Standalone adoption classifies a pre-inheritance eight-table code-index schema and applies `code_inheritance` without replaying `CREATE TABLE` against an already-provisioned external relation. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_adoption_preserves_pre_inheritance_and_skips_existing`.

### 1.2 Extract explicit heritage clauses [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/languages.rs::*` — scope-reason: add `inheritance_query`, update every `LanguageSpec` literal, and declare the heritage query module
- `crates/gcode/src/index/parser.rs::*` — scope-reason: call extract_inheritance from parse_file_with_semantic and keep the inheritance module declaration compiling
- `crates/gcode/src/index/parser/inheritance.rs`
- `crates/gcode/src/index/languages/heritage.rs`
- `crates/gcode/src/index/parser/calls/ast.rs::extract_js_calls`
- `crates/gcode/src/index/parser/tests.rs`
- `crates/gcode/src/index/parser/tests/heritage.rs`

Add `inheritance_query: &'static str` to `LanguageSpec`. Keep the field initializers in `languages.rs` as one-line `&HERITAGE_PYTHON` references. Put the tree-sitter query strings in new `crates/gcode/src/index/languages/heritage.rs`. Empty string means "this language emits no inheritance edges." The standalone `LanguageSpec { ... }` literal in `crates/gcode/src/index/parser/calls/ast.rs` (`extract_js_calls`) must set `inheritance_query: ""` in this same leaf or the crate does not compile.

`languages.rs` must stay under 1,000 lines (today 814). Do not paste per-language heritage queries into that file.

New `crates/gcode/src/index/parser/inheritance.rs` with `extract_inheritance(...)`, called from `parse_file_with_semantic` after symbols and imports exist so targets can resolve against the same-file symbol list and import bindings. Follow `extract_calls` / `materialize_call` for resolution tiers:

1. Same-file class / type / interface / trait / struct whose name equals the heritage name.
2. Import-bound local symbol (reuse the call extractor's import bindings; `LocalImport` then the existing local-import promotion pass if a second scan is required).
3. External module qualifier (stdlib / known crate) → `CallTargetKind::External`.
4. Otherwise `Unresolved`, keeping `target_name`.

Parser fixtures must materialize both target terminals, not only the Rust source terminals in 1.2.10: one External base (`target_kind`, `target_name`, `target_external_module`) and one Unresolved base (`target_kind`, `target_name`, empty target carrier).

Edge direction is always derived → base. Multiple bases emit one row each. Skip unnamed / empty captures.

Heritage kind by language (implement as a match, not a markdown work table):

- Python, JavaScript, Ruby: every listed base / superclass → `INHERITS`. Ruby `include` / `extend` / `prepend` → `IMPLEMENTS`.
- TypeScript, Java, C#, Kotlin, Swift, Scala, PHP, Dart, Objective-C, C++: class/struct `extends` / superclass / `: Base` where the target is a class → `EXTENDS`. `implements` / protocols / `with` mixins / ObjC protocols → `IMPLEMENTS`. Interface-extends-interface → `EXTENDS`.
- Rust: `impl Trait for Type` → `IMPLEMENTS` (source = Type, target = Trait). `trait Foo: Bar` → `EXTENDS` (source = Foo, target = Bar). Inherent `impl Type` emits nothing. `collapse_rust_impl_symbols` still runs for method parenting; heritage extraction must read the impl node **before** impl symbols are dropped, or walk the tree independently of the collapsed symbol list. Resolve the Type source with the same four `extract_calls` tiers already listed for targets — do not collapse every non-same-file Type into `LocalImport`. Same-file `Type` sets `source_kind = Symbol` and `source_symbol_id` and leaves `source_external_module` empty. In-repo cross-file `impl Trait for Type` (the common case `collapse_rust_impl_symbols` already documents: parent link dropped when the type is declared elsewhere) must **not** drop the edge: persist `source_name = Type`, `source_kind = LocalImport`, `source_external_module` = joined candidate files from the same import-resolution bindings used for targets, and leave `source_symbol_id` null until the 1.3 post-write promotion pass resolves Type to its canonical `code_symbols` row. Crate/stdlib Type (`impl LocalTrait for external_crate::ExternalType`, or an import binding that `materialize_call` would classify External) persists `source_kind = External` with the module qualifier and is **not** a LocalImport promotion candidate. An unresolved Type in partial code persists `source_kind = Unresolved` with `source_name` and is also not a promotion candidate. Trait targets still use the four target tiers and `target_external_module`. A single row may carry distinct source and target candidate sets at the same time. Add a two-file in-repo regression: `Type` in one file, `impl Trait for Type` in another. Add external-source and unresolved-source fixtures, including `impl LocalTrait for external_crate::ExternalType`.
- Go: struct embedding (`type Foo struct { Bar }`) and interface embedding (`type Foo interface { Bar }`) → `EXTENDS`. Do not infer implicit interface satisfaction.
- C, Elixir, Lua, Bash, YAML, JSON: empty query.

Queries capture the enclosing type name, each heritage name, and an optional kind discriminator (`extends` vs `implements`) when the grammar has one. If the grammar cannot distinguish class vs interface for a Kotlin/Swift `: Base` clause, emit `INHERITS` rather than guessing. Apply that same conservative fallback to C# `class_declaration` `base_list` entries: tree-sitter-c-sharp puts every class and interface base in one undifferentiated list. C# `interface_declaration` bases are `EXTENDS`. C# `struct_declaration` bases are `IMPLEMENTS` — a struct cannot inherit a class. C# `class_declaration` bases use `EXTENDS` or `IMPLEMENTS` only when a resolved **same-file** symbol kind proves class vs interface; imported and unresolved mixed bases stay `INHERITS`. Do not guess the first base is the superclass. Do not rewrite `heritage_kind` during LocalImport promotion. Do not add `record_declaration` extraction in this plan; C# records stay out unless a later leaf already indexes them as symbols.

Declare `#[path = "languages/heritage.rs"] mod heritage;` from `languages.rs` so `languages.rs` and `languages/heritage.rs` can coexist. Register `mod heritage;` in `crates/gcode/src/index/parser/tests.rs` next to the other parser test modules.

Tests live in `crates/gcode/src/index/parser/tests/heritage.rs` and use the existing `parse_python` / `parse_typescript` / `parse_rust` / `parse_go` / … helpers. One fixture pair per emitting language: a named derived type and a named base in the same file must produce the expected `HeritageKind` and resolved `target_symbol_id`. Rust fixture: `impl Display for Thing` and `trait Foo: Debug`. Go fixture: struct embed + interface embed. Empty-query languages parse without emitting rows. `languages.rs` stays below 1,000 lines after the field is added.

**Acceptance:**

- 1.2.1 - `LanguageSpec` has `inheritance_query` and query bodies live in `heritage.rs`. symbol: `LanguageSpec`.
- 1.2.2 - `parse_file_with_semantic` fills `ParseResult.inheritance`. symbol: `parse_file_with_semantic`.
- 1.2.3 - Same-file Python `class Derived(Base)` resolves `INHERITS` to `Base`. test: `crates/gcode/src/index/parser/tests/heritage.rs::python_subclass_resolves_same_file_base`.
- 1.2.4 - TypeScript `class D extends B implements I` emits `EXTENDS` and `IMPLEMENTS`. test: `crates/gcode/src/index/parser/tests/heritage.rs::typescript_extends_and_implements`.
- 1.2.5 - Rust `impl Trait for Type` and `trait Foo: Bar` emit `IMPLEMENTS` / `EXTENDS`. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_impl_and_supertrait`.
- 1.2.6 - Go embedding emits `EXTENDS`; a type that only happens to satisfy an interface does not. test: `crates/gcode/src/index/parser/tests/heritage.rs::go_embedding_only`.
- 1.2.7 - Every other emitting language has a same-file base/derived fixture in that test module. test: `crates/gcode/src/index/parser/tests/heritage.rs::all_emitting_languages_have_same_file_pair`.
- 1.2.8 - `parser/tests.rs` declares `mod heritage` so the new module compiles and runs. file: `crates/gcode/src/index/parser/tests.rs`.
- 1.2.9 - Cross-file Rust `impl Trait for Type` emits `IMPLEMENTS` with a retryable LocalImport source; same-file Type remains a resolved Symbol source. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_impl_source_resolves_across_files`.
- 1.2.10 - `impl LocalTrait for external_crate::ExternalType` persists `source_kind = External`; an unresolved Type persists `source_kind = Unresolved`; neither is a LocalImport promotion candidate. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_impl_external_and_unresolved_sources`.
- 1.2.11 - C# `interface` bases are `EXTENDS`, `struct` bases are `IMPLEMENTS`, and a class `base_list` with imported or unresolved mixed bases stays `INHERITS`; a same-file class vs interface target is typed. test: `crates/gcode/src/index/parser/tests/heritage.rs::csharp_base_list_uses_inherits_when_kind_unproven`.
- 1.2.12 - Ruby `include`, `extend`, and `prepend` each emit `IMPLEMENTS` (not `INHERITS`). test: `crates/gcode/src/index/parser/tests/heritage.rs::ruby_mixins_emit_implements`.
- 1.2.13 - Inherent Rust `impl Type` (no Trait) emits no heritage row. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_inherent_impl_emits_nothing`.
- 1.2.14 - C, Elixir, Lua, Bash, YAML, and JSON parse without emitting heritage rows. test: `crates/gcode/src/index/parser/tests/heritage.rs::empty_query_languages_emit_no_rows`.
- 1.2.15 - An imported-stdlib or crate-qualified base persists `target_kind = External` with `target_external_module`; an unbound name persists `target_kind = Unresolved` with `target_name` and an empty target carrier. Neither is a LocalImport promotion candidate. test: `crates/gcode/src/index/parser/tests/heritage.rs::heritage_external_and_unresolved_targets`.

### 1.3 Persist inheritance facts on index [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/api.rs::*` — scope-reason: add upsert_inheritance/insert_inheritance, CodeFactWriteSummary.inheritance, for_file arity, delete/promote/adopt siblings, and keep existing call writers compiling
- `crates/gcode/src/index/indexer/sink.rs::CodeFactSink`
- `crates/gcode/src/index/indexer/sink.rs::PostgresCodeFactSink`
- `crates/gcode/src/index/indexer/file.rs::write_parsed_file_facts`
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: RecordingCodeFactSink plus inheritance promotion/invalidation fixtures
- `crates/gcode/src/index/api_tests.rs::*` — scope-reason: existing insert-count test plus new inheritance insert-count test
- `crates/gcode/src/index/indexer/local_imports.rs::*` — scope-reason: keep LocalImport inheritance rows retryable and promote them with the same candidate-file rules as calls
- `crates/gcode/src/index/indexer/lifecycle.rs::attach_projection_sync`
- `crates/gcode/src/index/indexer/pipeline.rs::*` — scope-reason: keep promotion owner paths on IndexOutcome instead of discarding resolve_local_import_* returns
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: same promotion-owner threading on the overlay index path
- `crates/gcode/src/index/indexer/types.rs::IndexOutcome`
- `crates/gcode/src/commands/index.rs::*` — scope-reason: run-path wiring plus the focused provider-later graph-only owner projection regression
- `crates/gcode/src/projection/sync.rs::ProjectionSyncRequest`
- `crates/gcode/src/projection/sync.rs::ProjectionSyncStatus`
- `crates/gcode/src/projection/sync.rs::pending_after_code_fact_write`
- `crates/gcode/src/projection/sync.rs::sync_after_index`
- `crates/gcode/src/projection/sync.rs::sync_after_index_bounded`
- `crates/gcode/src/db/queries.rs::*` — scope-reason: add inheritance row mapping plus derived-file and project-wide pending LocalImport readers mirroring read_local_import_calls / read_project_local_import_calls, join active content, and CAS mark_graph_synced on the captured hash plus graph_sync_attempted_at
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: delete code_inheritance in cleanup and assert insert/count next to code_calls
- `crates/gcode/src/index/indexer/stale_cleanup_tests.rs::*` — scope-reason: project cleanup that deletes code_calls must also delete code_inheritance
- `crates/gcode/src/index/indexer/tests/api_contract.rs::*` — scope-reason: invalidate shared-fact retention must list code_inheritance next to code_calls
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: retarget source_inventory for the new inheritance write/read PostgreSQL call sites
- `tests/code_index/test_gcode_privilege_manifest.py::*` — scope-reason: source_inventory exact-set must match the new inheritance call sites

Mirror the call write path. Add `upsert_inheritance` / `insert_inheritance` next to `upsert_calls` / `insert_call` with the same `ON CONFLICT (...) DO NOTHING` and `NULLS NOT DISTINCT` unique key. Extend `delete_content_version_non_symbol_facts` to `DELETE FROM code_inheritance WHERE project_id = $1 AND file_path = $2 AND content_hash = $3`. Add `inheritance: usize` to `CodeFactWriteRequest` and `CodeFactWriteSummary`.

Extend `CodeFactSink` with `upsert_inheritance(...)`. Implement it on `PostgresCodeFactSink` and `RecordingCodeFactSink`. In `write_parsed_file_facts`, after `upsert_calls`, persist `parse_result.inheritance`.

Any indexer test that deletes `code_calls` for cleanup must also delete `code_inheritance`. That includes the project cleanup in `stale_cleanup_tests.rs`. The `api_contract.rs` invalidate shared-fact list must name `code_inheritance` next to `code_calls` as a retained shared fact. Update `api_tests` so an inserted inheritance row is counted the same way calls are (rows inserted, not input len). Incremental reindex of a file replaces that file+hash's inheritance rows via the existing content-version delete then insert. After adding production `query`/`execute` sites, retarget `managed_postgres_privileges.json` `source_inventory` so the exact-set Python contract stays current.

Import-bound bases **and** in-repo cross-file Rust impl sources are in-scope. External and Unresolved Rust impl sources persist as terminals and are not selected by the LocalImport readers. Extend the existing local-import promotion path so `LocalImport` inheritance rows rewrite `target_kind` and/or `source_kind` to `Symbol` on a hit, independently. A source hit clears `source_external_module` only; a target hit clears `target_external_module` only. Do not add a second project-wide resolver or a new provenance store.

Do **not** copy the call miss path that rebuilds an `Unresolved` row and drops candidate files. A miss stays `LocalImport` and keeps that side's `*_external_module` candidate carrier so a later index of the provider can retry. When a candidate file is indexed **or newly adopted**, run promotion for pending inheritance rows that name that file — not only rows whose `file_path` is in the changed set. `resolve_project_local_import_calls` is the call analog; inheritance needs the same "pending rows whose candidates just appeared" trigger. The promotion-trigger path set is this run's parsed files **plus** paths newly returned by `adopt_file_state` on the ordinary (`pipeline.rs`) and overlay (`overlay.rs`) paths. Adoption does not parse and must not enter `vector_file_paths` or become a graph-projection path by itself. A shared-content provider that becomes active only through adoption must still run the pending-inheritance readers so a stranded LocalImport consumer can promote. Do not add an overlay-composition contract.

Add sibling readers in `crates/gcode/src/db/queries.rs` — do not filter only on the derived row's `file_path` when the trigger is a newly indexed provider. Mirror the call pair: `read_local_import_inheritance` (derived-file pending) and `read_project_local_import_inheritance` (project-wide recovery). Both readers, and provider resolution, must join the **active** indexed file state (`code_indexed_file_states` plus matching `content_hash` / `file_content_hash`) the same way `read_symbols_for_file` already binds a content hash. Inactive retained rows must not create false ambiguity or promote to an old provider. Apply the existing overlay-shadow helpers before uniqueness. If promotion reuses `resolve_local_callee_symbol_id`, add that same active-content join there; do not add a second resolver or a new public or private visibility type. Map rows through an `inheritance_relation_from_row` helper that round-trips both carriers. Test consumer-first and provider-first index orders for both target bases and Rust impl sources, plus a row that is LocalImport on both ends with distinct candidate sets.

Index order: derived-before-base must promote when the base file is later indexed; base-before-derived must resolve the target on the derived write when the base is already present. The same two orders apply to Rust impl sources (Type-after-impl vs Type-before-impl). One `code_inheritance` row per (source, target, heritage_kind, line) — promotion updates that row, it does not insert a duplicate.

Promotion returns the distinct owning `file_path` values whose rows changed. Today `resolve_local_import_calls` returns `()` and `pipeline.rs` / `overlay.rs` discard that result; `IndexOutcome` exposes only `indexed_file_paths`; `ProjectionSyncRequest` has one `file_paths` list; and `commands/index.rs::run` passes `outcome.indexed_file_paths` into `sync_after_index_bounded`, which syncs **both** graph and vectors from that same list. Thread two lists end to end:

- `vector_file_paths` = this run's indexed files
- `graph_file_paths` = indexed files union promoted owners

Carry both on `IndexOutcome` and `ProjectionSyncRequest`. Replace `ProjectionSyncStatus.file_paths` with the same two lists (`graph_file_paths`, `vector_file_paths`). `pending_after_code_fact_write` copies each list from the request; it must not collapse them when `graph_pending` and `vectors_pending` are both true. `attach_projection_sync` builds that request and stores the split status on `IndexOutcome.projection_sync` (the public serialized wrapper). `sync_after_index` / `sync_after_index_bounded` take both lists (or read them from the request) and must not send promoted-only owners to Qdrant. `run` must use those lists — it must not pass `indexed_file_paths` to a function that syncs both backends. Do not add a second projection queue or a new public or private visibility type.

Those in-memory lists are not enough when `sync_projections` is false. `attach_projection_sync` still returns early in that case, and the daemon pending-recovery scan (`graph_synced IS FALSE`, `graph_sync_attempted_at` null or older than the cooloff) will skip an owner that is still marked graph-synchronized. In the **same database transaction** that promotes either inheritance endpoint, set `graph_synced = false` and `graph_sync_attempted_at = NULL` on each owning active `(project_id, file_path, content_hash)` row. Do not change `vectors_synced`. Do not call `mark_graph_sync_attempted` for this dirty — that helper stamps `graph_sync_attempted_at = NOW()` and would cool the owner out of recovery. Add a sibling helper next to `mark_graph_synced` / `reset_graph_sync_for_project` if no per-file reset exists. Overlay and ordinary index paths must share that write so a distinct overlay dirty is unnecessary. Immediate sync, when enabled, still graph-projects only the owner and must not vector-sync that owner-only path.

`mark_graph_synced` currently joins the live `code_indexed_file_states` row by path and marks whatever hash is current. Capture the exact `content_hash` **and** `graph_sync_attempted_at` the worker stamped for this attempt (`mark_graph_sync_attempted` already writes that timestamp; do not add a column). Completion is a compare-and-set on both still matching the live row. LocalImport promotion dirties `graph_synced = false` and `graph_sync_attempted_at = NULL` without changing the owner hash, so a same-hash H2 promotion invalidates an in-flight H1 completion. A failed compare-and-set is stale completion, not success: in the same statement or one transaction, set `graph_synced = false` and `graph_sync_attempted_at = NULL` on the **currently active** file row so pending recovery reprojects the live facts. Do not reverse individual Falkor rows. Do not add a generation lock, generation table, extra project lock, or a second projection queue. Do not change `mark_vectors_synced`.

After promotion to `Symbol`, invalidation matches calls. Do not add a provenance table or keep a second carrier. Reindex of the **derived** file content-version-deletes then reinserts inheritance from the new parse. Until that reindex, a renamed or deleted base may leave a stale `target_symbol_id` (same as calls). Graph rebuild / stale `sync_token` then drop edges that PostgreSQL no longer emits.

**Acceptance:**

- 1.3.1 - Indexing a file writes `code_inheritance` rows and deletes them with the content-version cleanup. symbol: `delete_content_version_non_symbol_facts`.
- 1.3.2 - `CodeFactSink` persists inheritance through the indexer file path. symbol: `CodeFactSink`.
- 1.3.3 - API insert reports inserted inheritance rows, not input length. test: `crates/gcode/src/index/api_tests.rs::api_upsert_inheritance_reports_rows_inserted_not_input_len`.
- 1.3.4 - Indexing a derived type that imports its base, then indexing the base file, promotes the same `LocalImport` row to `target_kind` Symbol with the canonical `target_symbol_id` and no duplicate. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_imported_base_promotes_after_base_indexes`.
- 1.3.5 - A miss leaves the row `LocalImport` with candidate files intact; a later base index promotes it. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_local_import_miss_stays_retryable`.
- 1.3.6 - Reindexing the derived file after the base is deleted or renamed rewrites the inheritance row the same way calls refresh; no extra provenance store. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_invalidates_on_derived_reindex`.
- 1.3.7 - Pending inheritance readers live in `crates/gcode/src/db/queries.rs` and select rows by derived file and by project-wide LocalImport recovery, including a later-indexed Rust impl source Type. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_source_promotes_when_type_file_indexes`.
- 1.3.8 - When the imported base is already indexed, writing the derived file resolves `target_kind` to Symbol with the canonical id and exactly one row. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_imported_base_resolves_when_base_already_indexed`.
- 1.3.9 - When the Rust Type file is already indexed, writing `impl Trait for Type` resolves `source_kind` to Symbol with the canonical id and exactly one row. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_impl_source_resolves_when_type_already_indexed`.
- 1.3.10 - A row that is LocalImport on both ends keeps distinct source and target candidate sets through insert and miss; promoting one side leaves the other carrier intact. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_keeps_independent_source_and_target_carriers`.
- 1.3.11 - Promoting a row owned by a file that was not in this run's indexed set unions that owning path into graph projection and not into vector projection. symbol: `attach_projection_sync`.
- 1.3.12 - `gcode index` with projections enabled, after a provider-later promotion, graph-syncs the owning file and does not vector-sync that owner-only path. test: `crates/gcode/src/commands/index.rs::index_promotion_projects_owner_on_graph_only`.
- 1.3.13 - After a graph-only owner promotion, `IndexOutcome.projection_sync` lists the owner in `graph_file_paths` only, while the newly indexed provider is in both lists. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_pending_status_keeps_graph_only_owner`.
- 1.3.14 - Provider-later promotion with `sync_projections=false` sets the owner's `graph_synced` to false and `graph_sync_attempted_at` to null and leaves `vectors_synced` unchanged, so a later pending-recovery scan can project the new typed edge. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_promotion_dirties_owner_graph_pending`.
- 1.3.15 - After the new inheritance write/read call sites land, `managed_postgres_privileges.json` `source_inventory` matches the production PostgreSQL call inventory. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_covers_every_rust_database_call_at_head`.
- 1.3.16 - Project cleanup that deletes `code_calls` also deletes `code_inheritance`. test: `crates/gcode/src/index/indexer/stale_cleanup_tests.rs::cleanup_project_deletes_code_inheritance`.
- 1.3.17 - Invalidate retains `code_inheritance` as a shared fact next to `code_calls`. test: `crates/gcode/src/index/indexer/tests/api_contract.rs::invalidate_postgres_deletes_only_machine_state`.
- 1.3.18 - An old retained provider symbol and an inactive pending LocalImport row do not create false ambiguity or promote against inactive content; only the active content hash participates. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_promotion_uses_active_content_only`.
- 1.3.19 - A late `mark_graph_synced` after the path's active hash changed does not mark the new hash synced. test: `crates/gcode/src/db/queries.rs::mark_graph_synced_cas_rejects_stale_hash`.
- 1.3.20 - After H2 projects hash B and CAS-succeeds, a late H1 Falkor write then failed CAS re-dirties the live B row (`graph_synced = false`, `graph_sync_attempted_at` null) so recovery reprojects B. test: `crates/gcode/src/db/queries.rs::mark_graph_synced_failed_cas_dirties_live_row`.
- 1.3.21 - Adopting an already-persisted shared-content provider (not parsed this run) promotes a pending LocalImport consumer; the adopted path is not added to `vector_file_paths`. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_adoption_promotes_pending_consumer`.
- 1.3.22 - Same-hash promotion: H1 reads pre-promotion facts, H2 promotes and projects the same owner hash, late H1 Falkor write then failed `(content_hash, graph_sync_attempted_at)` CAS re-dirties the live row so recovery keeps H2's typed edge. test: `crates/gcode/src/db/queries.rs::mark_graph_synced_cas_rejects_same_hash_stale_attempt`.

## P2: FalkorDB projection
`kind: framing`

**Goal**: File sync, rebuild, and delete treat inheritance edges as first-class graph facts beside `CALLS` and `IMPORTS`.

### 2.1 Project inheritance edges [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gcode/src/graph/code_graph/write/mutation.rs::*` — scope-reason: add the inheritance partition to `SyncFileMutation`, update every constructor, and keep the CALLS writer snippets used as the heritage endpoint template
- `crates/gcode/src/graph/code_graph/write/sync_plan.rs::*` — scope-reason: append inheritance batches and update every `SyncFileMutation` constructor in this file
- `crates/gcode/src/graph/code_graph/write.rs::sync_file`
- `crates/gcode/src/graph/code_graph/write.rs::sync_file_graph`
- `crates/gcode/src/commands/graph/lifecycle.rs::*` — scope-reason: pass an empty inheritance slice from `sync_file_graph` and every `CodeGraph::sync_file` caller so this leaf compiles; 2.2 replaces those placeholders with `facts.inheritance`
- `crates/gcode/src/projection/sync.rs::sync_graph_file`
- `crates/gcode/src/graph/report/queries.rs::*` — scope-reason: add inheritance rel types to every report edge pattern constant and query
- `crates/gcore/src/graph_analytics.rs::weight_for_kind`
- `crates/gcore/src/graph_analytics.rs::weight_for_kind_covers_observed_aliases_case_insensitively`
- `docs/contracts/shared-graph-schema.md`
- `crates/gcode/tests/contract.rs::*` — scope-reason: writer contract and graph_view_payload_keys_are_complete
- `crates/gcode/src/graph/code_graph/tests.rs::*` — scope-reason: assert inheritance MERGE sets sync_token and recovers owner-before-provider and parallel same-type facts

Add Cypher writers parallel to `ADD_SYMBOL_CALLS_CYPHER`:

```cypher
UNWIND $rows AS row
MERGE (source:CodeSymbol {id: row.source_id, project: $project})
ON CREATE SET source.name = row.source_name, source.updated_at = timestamp()
MERGE (target:CodeSymbol {id: row.target_id, project: $project})
ON CREATE SET target.name = row.target_name, target.updated_at = timestamp()
MERGE (source)-[r:EXTENDS {file: row.file_path, line: row.line, content_hash: $content_hash}]->(target)
SET r.provenance = $provenance,
    r.confidence = $confidence,
    r.source_system = $source_system,
    r.source_file_path = row.file_path,
    r.source_line = row.line,
    r.source_symbol_id = row.source_id,
    r.sync_token = $sync_token
```

Copy the CALLS writer, not a weaker subset: `MERGE` both `CodeSymbol` endpoints by id/project with the same `ON CREATE` placeholders as `ADD_SYMBOL_CALLS_CYPHER`. Do **not** `MATCH` endpoints — an owner-file sync before the target (or source Type) provider is indexed, or a temporarily absent graph definition, would yield no row and the later 1.3.11 / 2.2.10 promotion sync would still miss the typed edge. External and unresolved **targets and sources** use the corresponding CALLS `MERGE (…:ExternalSymbol …)` / `MERGE (…:UnresolvedCallee …)` terminals on whichever endpoint needs them. `impl LocalTrait for external_crate::ExternalType` is `ExternalSymbol -[:IMPLEMENTS]-> CodeSymbol`. Key every heritage relationship by `file`, `line`, and `content_hash`, matching CALLS; do not MERGE only on `content_hash` or two same-endpoint, same-type facts on different lines collapse and overwrite provenance. The CHG public payload still deduplicates by `(source, target, rel)` after the walk. Copy the CALLS property set, including `sync_token`. Stale-edge delete is token-only; a missing token is deleted as stale. One query per `HeritageKind` **or** one `UNWIND` that uses a parameterized relationship type if the existing typed-query helper allows it. If FalkorDB cannot parameterize the rel type, emit three named queries (`add_inherits_query`, `add_extends_query`, `add_implements_query`). Select the endpoint labels from the existing CALLS snippets (`add_symbol_calls_query` / `add_external_calls_query` / `add_unresolved_calls_query`); do not invent a second writer family.

Extend `SyncFileMutation` with an inheritance partition by `(source_kind, target_kind)` using those same three graph labels (`CodeSymbol`, `ExternalSymbol`, `UnresolvedCallee`). `LocalImport` rows are not projected until promotion rewrites that side to `Symbol`. `plan_sync_batches` appends those chunks after calls. This leaf also updates every `SyncFileMutation { ... }` constructor and the production `CodeGraph::sync_file`, free `write.rs::sync_file_graph` wrapper, `lifecycle.rs` callers, and `projection/sync.rs::sync_graph_file` signatures so they compile independently of 2.2: pass an empty inheritance slice until 2.2 threads PostgreSQL facts. Do not leave those callers on the old positional argument list.

Update `CODE_EDGE_REL_TYPES` / `CODE_EDGE_REL_PATTERN` in `crates/gcode/src/graph/report/queries.rs` from `DEFINES|IMPORTS|CALLS` to include `INHERITS|EXTENDS|IMPLEMENTS` so report degree queries do not ignore the new edges.

`weight_for_kind` already maps `extends` / `implements` to 2.5. Add `inherits` to that same arm so `INHERITS` is not the unknown-kind default of 1.0. Extend `weight_for_kind_covers_observed_aliases_case_insensitively` with case-insensitive `inherits` / `INHERITS` → 2.5. Do not invent a second weight table. MCG still runs Leiden only on the scoped IMPORTS subgraph (section 3.3). Stale-token deletion is implemented in §2.2, not this leaf.

Update `docs/contracts/shared-graph-schema.md` in the same leaf: document `INHERITS`, `EXTENDS`, and `IMPLEMENTS` the same way `CALLS` is documented, but allow `CodeSymbol|ExternalSymbol|UnresolvedCallee` on **either** endpoint (`CodeSymbol -> CodeSymbol`, `CodeSymbol -> ExternalSymbol`, `CodeSymbol -> UnresolvedCallee`, and the reverse source-terminal forms), with file, line, `source_file_path`, `source_symbol_id`, provenance, source system, `content_hash`, and `sync_token`. Update `code_graph_writer()` in `crates/gcode/tests/contract.rs` so the existing shared-schema contract sees the new Cypher strings. Memory: when write Cypher moves into new files, `code_graph_writer()` must `include_str!` them. Do not add a second contract harness.

**Acceptance:**

- 2.1.1 - File sync MERGEs inheritance relationships tagged with `content_hash` and `sync_token`. symbol: `plan_sync_batches`.
- 2.1.2 - Report edge patterns include `INHERITS|EXTENDS|IMPLEMENTS`. file: `crates/gcode/src/graph/report/queries.rs`.
- 2.1.3 - Writer contract includes the new Cypher. test: `crates/gcode/tests/contract.rs::code_graph_writer_matches_shared_schema_contract`.
- 2.1.4 - Case-insensitive `INHERITS` / `inherits` map to 2.5 in `weight_for_kind` alongside `EXTENDS` and `IMPLEMENTS`; unknown kinds stay 1.0. test: `crates/gcore/src/graph_analytics.rs::weight_for_kind_covers_observed_aliases_case_insensitively`.
- 2.1.5 - `docs/contracts/shared-graph-schema.md` names `INHERITS`, `EXTENDS`, and `IMPLEMENTS` with `CodeSymbol|ExternalSymbol|UnresolvedCallee` on either endpoint and the provenance fields above; the existing writer contract still passes. file: `docs/contracts/shared-graph-schema.md`.
- 2.1.6 - Owner-before-target-provider and owner-before-Rust-source-provider file syncs MERGE endpoints and recover the typed edge without a graph rebuild. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_recovers_when_owner_syncs_before_provider`.
- 2.1.7 - Two same-endpoint, same-type inheritance facts on different lines survive repeated sync as two relationships; CHG still public-dedups by `(source, target, rel)`. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_keeps_parallel_same_type_facts`.
- 2.1.8 - An `External` or `Unresolved` Rust impl source MERGEs `ExternalSymbol` / `UnresolvedCallee` as the **source** endpoint and the typed `IMPLEMENTS` edge survives file sync. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_external_and_unresolved_sources`.
- 2.1.9 - A `CodeSymbol` source with an `External` or `Unresolved` base MERGEs `CodeSymbol → ExternalSymbol` and `CodeSymbol → UnresolvedCallee` through `plan_sync_batches`, with the typed relationship, owner file/line, `content_hash`, and `sync_token`. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_external_and_unresolved_targets`.
- 2.1.10 - `write.rs::sync_file_graph`, `lifecycle.rs` graph callers, and `projection/sync.rs::sync_graph_file` compile against the new `sync_file` arity by passing `&[]` until 2.2. symbol: `sync_file_graph`.

### 2.2 Delete and rebuild inheritance edges [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/graph/code_graph/write/deletion.rs::delete_file_graph_queries`
- `crates/gcode/src/graph/code_graph/write/deletion.rs::delete_stale_file_graph_queries`
- `crates/gcode/src/graph/code_graph/write/deletion.rs::delete_content_version_queries`
- `crates/gcode/src/graph/code_graph/write/deletion.rs::cleanup_orphans_cypher_segments`
- `crates/gcode/src/graph/code_graph/tests.rs::*` — scope-reason: assert inheritance rels on every existing delete/rebuild Cypher fixture
- `crates/gcode/src/graph/code_graph/write.rs::sync_file`
- `crates/gcode/src/graph/code_graph/write.rs::sync_file_graph`
- `crates/gcode/src/db/queries.rs::*` — scope-reason: GraphFileFacts, read_graph_file_facts, inheritance pending-row readers, and an active visible `(source_file, target_module)` reader for MCG seed equivalence
- `crates/gcode/src/commands/graph/lifecycle.rs::*` — scope-reason: inheritance-aware `has_no_graph_facts`, pass the captured hash and attempt timestamp into `mark_graph_synced` from `sync_file_graph` and `rebuild_project_graph`, and keep existing lifecycle tests compiling
- `crates/gcode/src/commands/graph/tests.rs::*` — scope-reason: update every `has_no_graph_facts` call site and assert an inheritance-only slice is not empty
- `crates/gcode/src/projection/sync.rs::sync_graph_file`
- `crates/gcode/src/commands/status/content_gc.rs::discover_content_gc`
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: existing GC fixtures plus an inheritance-only content-version has_graph_facts regression

Heritage deletion keys on the **relationship owner**, not on the source symbol's declaring file. Cross-file Rust `impl Trait for Type` is owned by the impl file (`r.file` / `r.source_file_path`) while the source Type lives elsewhere. Do **not** copy whole-file CALLS (`MATCH (s:CodeSymbol {file_path: $file_path})-[r]->()`). Copy the stale-token and content-version CALLS predicate that already uses relationship provenance:

```cypher
MATCH (s {project: $project})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})
WHERE (r.file = $file_path OR r.source_file_path = $file_path)
DELETE r
```

Do **not** require a `CodeSymbol` source label. An `ExternalSymbol` or `UnresolvedCallee` source (Rust `impl LocalTrait for external_crate::T`) would miss the owner-file delete. Bind `{project: $project}` on both generic endpoints, matching stale-token CALLS (`CodeSymbol {project}` → `{project}`). A same-path owner in another project must not lose its edge. Filter on relationship provenance (`r.file` / `r.source_file_path`), then delete `r`.

Apply that ownership predicate **and** both-endpoint project bind to whole-file, stale-token, and content-version deletes. Content-hash scoped deletes also require `r.content_hash = $content_hash`. Stale-token deletes also require `r.sync_token IS NULL OR r.sync_token <> $sync_token`. Orphan cleanup must not leave a `CodeSymbol` that is only reachable via inheritance, and must not delete a symbol that still has an inheritance edge from another live file.

Rebuild is "delete then plan_sync_batches" and must pick up inheritance from PostgreSQL the same way it loads calls. A later-indexed provider that promotes an inheritance row owned by a previously indexed impl or derived file must graph-sync that owning file (section 1.3.11) so FalkorDB shows the typed edge without `gcode graph rebuild` or reindexing the owner.

Carry inheritance through the production loader, not a side path:

- Add `inheritance: Vec<InheritanceRelation>` to `GraphFileFacts`.
- `read_graph_file_facts` SELECTs `code_inheritance` for that project/file.
- Add a sibling of `read_imports_for_file` that returns active visible `(source_file, target_module)` pairs for the project, using the same active-content join as other readers. Section 3.3 calls that reader for MCG seed equivalence. Do not add a `code_imports` column and do not set a resolved `provider_file` property on `IMPORTS` edges.
- `has_no_graph_facts` is true only when imports, definitions, calls, **and** inheritance are all empty. An inheritance-only file must sync, not `sync_no_fact_file`.
- Content-GC `discover_content_gc` currently ORs `code_symbols`, `code_imports`, and `code_calls` into `has_graph_facts`. Include `code_inheritance` in that same EXISTS predicate so an inheritance-only content version still deletes its FalkorDB edges before the PostgreSQL row is dropped. Add an inheritance-only content-version regression in the existing content-GC test module.
- `CodeGraph::sync_file` / `sync_file_graph` and `lifecycle::sync_file_graph` pass the inheritance slice into `SyncFileMutation`.
- `projection/sync.rs::sync_graph_file` is the normal post-index projection path (not only the graph lifecycle CLI). It currently passes `facts.imports`, `facts.definitions`, and `facts.calls` positionally. Pass `facts.inheritance` through that same call. Pass the captured `facts.content_hash` and the attempt's `graph_sync_attempted_at` into the 1.3 compare-and-set `mark_graph_synced`. Add a focused regression that an inheritance-only file is projected outside the graph CLI.

`cleanup_orphans_cypher_segments` currently treats `ExternalSymbol` / `UnresolvedCallee` as live only when referenced by `CALLS`, and detached `CodeSymbol` nodes only when referenced by `DEFINES` or `CALLS`. Heritage-only terminals required by 3.4 must survive cleanup: treat `INHERITS|EXTENDS|IMPLEMENTS` as live references on `CodeSymbol`, `ExternalSymbol`, and unresolved nodes. Add a lifecycle test that projects a heritage-only external or unresolved terminal, runs cleanup, and still queries the typed edge.

Existing graph write tests that assert combined delete Cypher contain `[r:CALLS]` must also assert the inheritance pattern. Add a unit test that a rebuilt file with one `EXTENDS` row produces that rel type in the planned queries. A promoted LocalImport inheritance row (section 1.3) must project the same typed heritage relationship as a same-file Symbol row. After a derived-file reindex that drops or retargets a heritage row, stale-token delete removes the previous edge.

**Acceptance:**

- 2.2.1 - File and content-hash deletes remove inheritance relationships. symbol: `delete_file_graph_queries`.
- 2.2.2 - Rebuild plans inheritance edges from PostgreSQL facts loaded by `read_graph_file_facts`. symbol: `read_graph_file_facts`.
- 2.2.3 - Combined delete Cypher includes inheritance rels. test: `crates/gcode/src/graph/code_graph/tests.rs::delete_queries_include_inheritance_rels`.
- 2.2.4 - Rebuild of a promoted imported-base inheritance row plans the expected typed heritage relationship. test: `crates/gcode/src/graph/code_graph/tests.rs::rebuild_projects_promoted_inheritance_edge`.
- 2.2.5 - An inheritance-only file is not treated as `no_graph_facts` and is synced. symbol: `has_no_graph_facts`.
- 2.2.6 - Derived-file reindex or rebuild after heritage rows change deletes previous inheritance edges whose `sync_token` no longer matches, including a missing token. test: `crates/gcode/src/graph/code_graph/tests.rs::rebuild_drops_stale_inheritance_after_derived_reindex`.
- 2.2.7 - `projection/sync.rs::sync_graph_file` projects inheritance facts; an inheritance-only file syncs outside the graph lifecycle CLI. symbol: `sync_graph_file`.
- 2.2.8 - Orphan cleanup retains `CodeSymbol`, `ExternalSymbol`, and unresolved nodes that remain live only through `INHERITS|EXTENDS|IMPLEMENTS`. test: `crates/gcode/src/graph/code_graph/tests.rs::cleanup_keeps_heritage_only_terminals`.
- 2.2.9 - Deleting or rebuilding the impl file removes the `IMPLEMENTS` edge even when the source Type is declared in another file. test: `crates/gcode/src/graph/code_graph/tests.rs::delete_cross_file_rust_impl_uses_relationship_owner`.
- 2.2.10 - Indexing the provider later, with projection enabled, projects the promoted typed heritage edge without a graph rebuild or owner-file reindex. test: `crates/gcode/src/graph/code_graph/tests.rs::promotion_projects_owning_file_without_rebuild`.
- 2.2.11 - Deleting the impl file removes an `IMPLEMENTS` edge whose source is `ExternalSymbol` or `UnresolvedCallee`; orphan cleanup still keeps a heritage-only source terminal that remains referenced. test: `crates/gcode/src/graph/code_graph/tests.rs::delete_external_source_impl_uses_relationship_owner`.
- 2.2.12 - An inheritance-only content version is `has_graph_facts=true` and participates in projection deletion before PostgreSQL GC. test: `crates/gcode/src/commands/status/content_gc/tests.rs::inheritance_only_content_has_graph_facts`.
- 2.2.13 - Whole-file, stale-token, and content-version heritage deletes bind `{project: $project}` on both generic endpoints and do not delete a colliding same-path edge in another project. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_delete_binds_project_on_both_endpoints`.

## P3: View surface
`kind: framing`

**Goal**: One scoped `gcode graph view` command returns FCG, MCG, or CHG as JSON plus validated Mermaid; `gcode callees` is the missing outgoing-1-hop primitive.

### 3.1 Add shared graph view payload and Mermaid [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::GraphCommand`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/commands/graph.rs`
- `crates/gcode/src/commands/graph/view/mod.rs`
- `crates/gcode/src/commands/graph/view/render.rs`
- `crates/gcode/src/commands/graph/view/fcg.rs`
- `crates/gcode/src/commands/graph/view/mcg.rs`
- `crates/gcode/src/commands/graph/view/class_hierarchy.rs`
- `crates/gcode/src/commands/graph/reads.rs::*` — scope-reason: expose resolve_symbol as pub(super) for the view scaffold without growing callers/callees logic
- `crates/gcode/src/visibility.rs::visible_graph_paths`
- `crates/gcode/src/codewiki_facts/graph.rs::CodewikiFacts::scoped_edges`
- `crates/gcode/src/codewiki_facts/graph.rs::CodewikiFacts::edges`
- `crates/gcode/src/codewiki_facts/graph.rs::edge_queries_apply_scope_before_ordered_limit`
- `crates/gcode/src/codewiki_facts/graph.rs::CodewikiFacts::graph_availability`
- `crates/gcode/src/codewiki_facts/graph.rs::GraphAvailability`
- `crates/gcode/src/codewiki_facts/graph.rs::classify_overfetch`
- `crates/gcode/src/codewiki_facts/graph.rs::take_bounded`
- `crates/gcode/src/codewiki_facts/graph_query.rs::*` — scope-reason: Incident incoming complete-frontier membership, DISTINCT public-edge LIMIT, eligible refill including already-emitted exclude, and the not-yet-indexed cross-chunk, distinct-pair, and exclude-set regressions
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: graph view clap domain, unknown view, omitted-depth defaults, and CHG row-limit rejection tests named in acceptance

Add `mod view;` to `crates/gcode/src/commands/graph.rs` (the view directory, not one flat view module) and re-export what dispatch needs. Split the surface so no new or touched production file crosses 1,000 lines:

- `crates/gcode/src/commands/graph/view/mod.rs` — `ViewKind`, clap/dispatch glue, shared seed resolution, the visibility-before-limit contract
- `crates/gcode/src/commands/graph/view/render.rs` — JSON + Mermaid payload and deterministic renderer
- `crates/gcode/src/commands/graph/view/fcg.rs` — filled by 3.2
- `crates/gcode/src/commands/graph/view/mcg.rs` — filled by 3.3
- `crates/gcode/src/commands/graph/view/class_hierarchy.rs` — filled by 3.4

Do not grow `payload.rs` (96 lines), `cli.rs` (439), or `reads.rs` (912) past the ceiling; keep clap shape in `GraphCommand` and callers/callees in `reads.rs`.

In this leaf, change `reads.rs::resolve_symbol` from private `fn` to `pub(super)` (or extract an equally small shared resolver module next to `reads.rs` if visibility requires it). The 3.1 view scaffold is the only consumer that needs the seam; 3.2 FCG and 3.4 CHG inherit it through this leaf. Do not wait on 3.2 to expose the resolver.

Add `GraphCommand::View` with `--view` required (`fcg` | `mcg` | `class-hierarchy`), a seed argument (symbol query for fcg/chg, file or module for mcg), and `--depth` as an **optional** clap `value_parser` `1..=16` (`MAX_SYMBOL_PATH_DEPTH`; reject 0 and values above 16). Do not give `--depth` a materialized clap default. After `ViewKind` resolution, omitted `--depth` becomes **8** for `class-hierarchy` and **1** for `fcg` / `mcg`. The payload `depth` field is that effective value. Incoming/outgoing limit flags (`--incoming-limit` / `--outgoing-limit`, defaults matching #18786 / neighbors) apply only to `fcg` and `mcg`. Passing either flag with `--view=class-hierarchy` is a clap / user error. Wire it in `dispatch.rs::run` after freshness checks.

Response schema (JSON object, also used for `--format text` plus a trailing Mermaid fence):

```json
{
  "project_id": "<resolved-project-id>",
  "project_root": "/abs/path/to/project",
  "view": "class-hierarchy",
  "seed": {"id": "<symbol-id>", "name": "Derived", "kind": "class", "file": "src/a.py"},
  "depth": 8,
  "incoming_truncated": false,
  "outgoing_truncated": false,
  "hint": null,
  "nodes": [{"id": "...", "name": "...", "kind": "class", "file": "src/a.py", "community": null}],
  "edges": [{"source": "...", "target": "...", "rel": "EXTENDS"}],
  "communities": [],
  "mermaid": "```mermaid\nflowchart TB\n...\n```"
}
```

Every successful view payload identifies the resolved project with `project_id` and `project_root`. Both envelope fields are required (same governing CLI rule as `docs/contracts/gcode-cli.md`). Callers-style `PagedResponse` stays `project_id` only; do not invent a second envelope type.

`nodes[].file` is nullable. Populate it for `CodeFile` and `CodeSymbol` from the declaring path. For a `CodeModule` with exactly one resolved provider file, use that provider path. Use `null` for `ExternalSymbol`, `UnresolvedCallee`, and a module with zero or several providers. Do not copy relationship-owner provenance onto a terminal or module node — that would claim the node is declared in the importing or deriving file. Symbol seeds keep a file path; module seeds may have `file: null`.

Reuse the existing callers unavailable path (`graph_read_or_empty` / `hint_for_error` / `CodewikiFacts::graph_availability`). An unconfigured or unreachable FalkorDB is not an available empty graph: emit empty `nodes` / `edges` plus a non-null `hint` (same strings callers already use). A configured available graph with no in-scope edges is empty `nodes` / `edges` and `hint: null`. Do not add a new availability enum or payload field beyond that optional `hint`. Missing seed remains a user error, not an empty graph.

`resolve_symbol_candidates` currently prints a PostgreSQL connect failure and returns `Ok((None, []))`, which callers treat as a missing symbol. Graph-view seed resolution must not use that collapse. Propagate connect and query errors from `db::connect_readonly` / `resolve_symbol_with_connection` as infrastructure failures. Reserve the user-error branch for a successful read with no unique symbol (missing or ambiguous). Do not invent a second resolver type: change the shared helper or call `resolve_symbol_with_connection` after a successful connect. A PostgreSQL outage is not a missing-seed message and is not a Falkor empty graph.

Always populate `mermaid` with the complete fence. Canonical node keys are typed and injective across kinds: `symbol:<uuid>`, `file:<path>`, `module:<name>`, `external:<id>`, `unresolved:<id>`. Assign opaque Mermaid IDs `n0`, `n1`, … from that sorted typed key set. Use the same typed keys as JSON `nodes[].id`, edge endpoints, renderer sort tokens, and `AnalyticsGraph` node/edge ids. Keep escaped display labels as the raw path, name, or uuid. Do not derive the identifier or the canonical key by sanitizing a UUID, path, or module name — a `CodeFile.path` equal to a `CodeModule.name` must stay two nodes. Hostile punctuation and two keys that normalize to the same token must still be distinct. Edge labels come from `rel`. No raw quotes, brackets, or newlines that break Mermaid. Sort `nodes` by canonical id and `edges` by `(source, target, rel)` so shuffled backend row order produces byte-identical JSON (canonical field order after that sort). Validate with `is_valid_mermaid` before printing; if validation fails, return an error — do not emit a broken fence and do not slice a valid fence to a character or token budget. One shared final-output regression must render a view payload whose JSON plus Mermaid exceeds 10,000 characters (the historical clip threshold). The printed output must parse as complete JSON, retain every expected node and edge, include the complete Mermaid fence, and pass `is_valid_mermaid`. Cover `fcg`, `mcg`, and `class-hierarchy` through that shared formatter. Do not add `--token-budget`. Do not add three live-view mega-fixtures.

Seed resolution calls the 3.1-exposed `resolve_symbol` (name or UUID → canonical symbol id) through the fallible path above. A successful read with no unique symbol is a user error, not an empty graph.

`GraphScopeMode::Incident` incoming currently emits `target IN [chunk]` plus `NOT source IN [chunk]`. That per-chunk exclusion misclassifies an internal frontier edge whose endpoints land in different `SCOPE_CHUNK_LEN` (64) chunks as incoming, so the row can consume the incoming limit and set `incoming_truncated` before later merge/dedup. Closed mode already keeps cross-chunk internals via `peer_keys`. Incoming Incident must use the **complete** typed frontier for membership: chunk only the transport `IN` list, then drop sources that are in the full frontier `HashSet` (or pass the full membership through the existing `peer_keys` exclusion only when that list stays within the same size budget that forced chunking).

A `LIMIT limit+1` on the raw Falkor relationship stream is not an eligible-edge bound. CALLS is merged per `{file, line, content_hash}`, so one public `(source, target, CALLS)` pair can occupy many relationships; IMPORTS can similarly repeat a public pair. `query_plan_rows` currently feeds raw `(source, target)` rows to `classify_overfetch`, which truncates and sets `Truncated` from that raw count; `take_bounded` only `dedup`s afterward. After the complete-frontier HashSet filter, those ineligible internals are gone but the sentinel window is already spent.

Reuse the existing callers/callees `RETURN DISTINCT source, target` pattern in `return_clause` so the Cypher `ORDER BY source, target LIMIT limit+1` counts distinct public pairs of the query kind. Then, in `scoped_edges` / the shared planner, refill each incoming Incident chunk with the existing total order `(source, target)` (keyset continuation, not `OFFSET`) after HashSet filtering until the chunk has `limit+1` distinct eligible rows or the query is exhausted. Multi-hop FCG/MCG walkers pass the walk-global emitted-edge set into that same refill. Already-emitted public `(typed source, typed target, rel)` pairs are ineligible before the quota, the same way complete-frontier internals are. Refill until `limit+1` **new** eligible pairs or exhaustion. Classify `incoming_truncated` / `outgoing_truncated` only after the global merge of those distinct eligible rows and `take_bounded`. Do not classify truncation from the raw relationship fetch count or from an already-emitted pair that sorted first. Thread an optional exclude set through the existing `scoped_edges` / planner; do not add a second query planner or a new edge-identity type. CHG has no row `LIMIT` and does not use this exclude set for sentinel counting.

Before any FCG, MCG, or CHG walk, build the active visible owner set from the existing visibility helpers (`visible_graph_paths` / overlay-shadow and machine-view already implemented there). Those helpers already apply machine, project, content-hash, and overlay-shadow internally; do not invent a second visibility store or a new public or private `(project_id, file_path, content_hash)` type. Apply that set **before** every FCG/MCG `LIMIT` (`EdgeQueryPlan` / `scoped_edges` currently `LIMIT limit+1` then return only source/target) and before every CHG page/hop-cut decision. Do **not** post-filter after the limit sentinel or after classifying `incoming_truncated` / `outgoing_truncated`. Invisible stale-hash, other-machine, or overlay-shadowed rows must not consume an edge slot or flip a direction or hop-cut flag. Seed visibility is not enough. External and unresolved terminals stay only when their owning relationship is visible. Do not invent a new graph-generation snapshot; existing callers/usages already read a live graph, and this plan does not add a generation table or extra project lock.

This leaf may stub the three view handlers behind a shared `ViewKind` enum as long as 3.2–3.4 replace the stubs in the same plan; alternatively land only the clap + payload + Mermaid helper and have 3.2–3.4 fill the kinds. Prefer the helper + empty-kind error (`unknown view`) so 3.2–3.4 stay additive. Acceptance 3.1.9 and 3.1.10 prove the shared visibility-before-bound helper with synthetic edges; they must pass while FCG, MCG, and CHG handlers are still stubs. Live-view application lives in 3.2, 3.3, and 3.4.

**Acceptance:**

- 3.1.1 - `gcode graph view` exists on `GraphCommand` and is dispatched. symbol: `GraphCommand`.
- 3.1.2 - Every successful view payload includes a complete Mermaid block that passes `is_valid_mermaid`. symbol: `is_valid_mermaid`.
- 3.1.3 - Invalid `--view` is a clap / user error. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_rejects_unknown_kind`.
- 3.1.4 - One shared renderer matrix for fcg, mcg, and class-hierarchy: shuffled input is byte-identical after sort; quote, bracket, newline, UUID-like, and non-ASCII labels escape; colliding sanitized keys stay distinct via opaque `n0`/`n1` IDs; file and module nodes are included; the mermaid field is a complete fenced block accepted by `is_valid_mermaid`. test: `crates/gcode/src/commands/graph/view/render.rs::view_render_is_deterministic_and_escapes_hostile_labels`.
- 3.1.5 - `--depth` is optional `1..=16`; 0 and 17 are clap errors; 16 is accepted. Omitted `--depth` is effective 8 for `class-hierarchy` and 1 for `fcg` and `mcg`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_depth_domain`.
- 3.1.6 - `--incoming-limit` or `--outgoing-limit` with `--view=class-hierarchy` is rejected; both are accepted for `fcg` and `mcg`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_rejects_hierarchy_row_limits`.
- 3.1.7 - `commands/graph.rs` declares `mod view` and dispatch reaches the view command. file: `crates/gcode/src/commands/graph.rs`.
- 3.1.8 - Typed canonical ids keep a file path that equals a module name as two nodes through JSON, Mermaid tokens, and `AnalyticsGraph` dedup. test: `crates/gcode/src/commands/graph/view/mod.rs::view_typed_ids_keep_file_and_module_collision_distinct`.
- 3.1.9 - The shared visibility-before-bound helper, given synthetic edges, drops stale-hash, other-machine, and overlay-shadowed endpoints; terminals inherit visibility from their owning visible relationship. Live FCG, MCG, and CHG handlers may still be stubs. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_respects_active_visible_file_map`.
- 3.1.10 - Invisible synthetic rows ordered before enough visible rows do not consume an edge slot or hop-cut; parent-only and same-path overlay-shadowed edges are classified before `LIMIT` / page exhaustion. Live view handlers may still be stubs. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_invisible_rows_do_not_consume_edge_or_hop_budget`.
- 3.1.11 - `crates/gcode/src/commands/graph/view/mod.rs`, `crates/gcode/src/commands/graph/view/render.rs`, `crates/gcode/src/commands/graph/view/fcg.rs`, `crates/gcode/src/commands/graph/view/mcg.rs`, `crates/gcode/src/commands/graph/view/class_hierarchy.rs`, and `crates/gcode/src/commands/graph/reads.rs` each stay under 1,000 lines. file: `crates/gcode/src/commands/graph/view/mod.rs`.
- 3.1.12 - `resolve_symbol` is `pub(super)` (or a sibling shared resolver) so 3.2 and 3.4 consume it without editing an undeclared file. symbol: `resolve_symbol`.
- 3.1.13 - An Incident incoming frontier larger than `SCOPE_CHUNK_LEN` with more than `incoming_limit+1` cross-chunk internal edges ordered before genuine external incoming edges still returns those external edges, does not count the internals toward the incoming limit, and does not set `incoming_truncated` unless distinct eligible overflow remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::incident_incoming_excludes_cross_chunk_frontier_sources`.
- 3.1.14 - More than `limit+1` parallel storage relationships for one public `(source, target, rel)` pair ordered before other distinct public pairs yield every distinct eligible neighbor and set a direction flag only when distinct eligible overflow remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::scoped_limits_count_distinct_public_edges`.
- 3.1.15 - A shared formatter payload whose JSON plus Mermaid exceeds 10,000 characters parses as complete JSON, retains every expected node and edge for fcg, mcg, and class-hierarchy, includes the complete Mermaid fence, and passes `is_valid_mermaid`. test: `crates/gcode/src/commands/graph/view/render.rs::view_render_does_not_clip_above_historical_budget`.
- 3.1.16 - A scoped incoming fetch whose first-ordered public pair is in the exclude set still returns a later new eligible neighbor, does not count the excluded pair toward the limit, and does not set `incoming_truncated` unless new eligible overflow remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::scoped_limits_exclude_already_emitted_edges`.
- 3.1.17 - A scoped outgoing fetch whose first-ordered public pair is in the exclude set still returns a later new eligible neighbor, does not count the excluded pair toward the limit, and does not set `outgoing_truncated` unless new eligible overflow remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::scoped_limits_exclude_already_emitted_outgoing_edges`.
- 3.1.18 - Every successful view payload includes `project_id` and `project_root` identifying the resolved project. test: `crates/gcode/src/commands/graph/view/render.rs::view_payload_includes_project_identity`.
- 3.1.19 - Unconfigured and unreachable FalkorDB views set a non-null `hint` and empty nodes/edges; a configured empty graph has `hint: null`. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_unavailable_differs_from_empty`.
- 3.1.20 - `nodes[].file` is populated for CodeFile/CodeSymbol, uses the unique provider path for a uniquely resolved module, and is null for ExternalSymbol, UnresolvedCallee, and ambiguous or unowned modules. test: `crates/gcode/src/commands/graph/view/render.rs::view_node_file_nullability_by_kind`.
- 3.1.21 - A PostgreSQL connect or query failure during seed resolution is an infrastructure error; a successful empty or ambiguous read is the missing-seed user error, including the allow-stale dispatch path. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_seed_resolution_propagates_database_errors`.

### 3.2 Add FCG view and callees command [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::Command`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/dispatch.rs::service_config_selection`
- `crates/gcode/src/dispatch/tests.rs::graph_and_vector_commands_request_only_needed_services`
- `crates/gcode/src/cli/tests/top_level.rs::*` — scope-reason: callers parse regression plus new callees top-level parse test
- `crates/gcode/src/commands/graph.rs`
- `crates/gcode/src/commands/graph/reads.rs::*` — scope-reason: add callees beside callers and share symbol-query resolution
- `crates/gcode/src/commands/graph/tests.rs::*` — scope-reason: callees pagination and >100-page tests named in acceptance
- `crates/gcode/src/commands/graph/view/fcg.rs`
- `crates/gcode/src/graph/code_graph.rs`
- `crates/gcode/src/graph/code_graph/read.rs`
- `crates/gcode/src/graph/code_graph/read/relationships.rs::*` — scope-reason: add production count_callees/find_callees next to callers and keep existing callers APIs
- `crates/gcode/src/graph/code_graph/read/relationship_queries.rs::*` — scope-reason: add find_callees_query next to find_callers_query
- `crates/gcode/src/codewiki_facts/graph_query.rs::*` — scope-reason: widen Call match_clause partitions and keep the 3.1 Incident membership, DISTINCT-limit, eligible-refill, and already-emitted exclude contract
- `crates/gcode/src/visibility.rs::filter_visible_graph_results`

`gcode callees <query>` mirrors `gcode callers`: same limit/offset flags, same symbol resolution, same `count_*` + `find_*(offset, limit)` shape. Do **not** implement the CLI on `find_callees_batch` — that helper is `#[cfg(test)]`-exported, has no offset, and is clamped by `MAX_GRAPH_LIMIT` (100). Add production `count_callees` and `find_callees` next to `count_callers` / `find_callers`, plus `find_callees_query` next to `find_callers_query`, and export them from `read.rs` and `code_graph.rs` unconditionally. Re-export `callees` from `crates/gcode/src/commands/graph.rs` next to `callers`. Add `Command::Callees` next to `Command::Callers` and dispatch it the same way.

`service_config_selection` is an exhaustive `match` on `Command`. `Command::Callees` must select `ServiceConfigSelection::falkordb_only()` next to `Command::Callers`. Update `crates/gcode/src/dispatch/tests.rs` (`graph_and_vector_commands_request_only_needed_services`) for service selection. Add `crates/gcode/src/cli/tests/top_level.rs` `test_parse_callees_remains_top_level` beside the callers parse test. Parsing and dispatch are separate criteria (3.2.6 and 3.2.10). Acceptance 3.2.1 cites the new `count_callees` / `find_callees` APIs, not `find_callers`.

`filter_visible_graph_results` treats any UUID `result.id` as a `code_symbols` row via `visible_symbols_by_ids`. External and unresolved callees use UUID ids from `make_external_symbol_id` / `make_unresolved_callee_id` and are therefore dropped. Make the visibility seam kind-aware: local `CodeSymbol` ids stay on the symbol filter; `ExternalSymbol` / `UnresolvedCallee` results stay visible when the **caller** symbol (or their file path) is visible. Test local, external, and unresolved callee targets.

Do not inherit `usages`/`search` output-clip `--token-budget` (`trim_results`). Pagination is a capped return, not a slice of a larger rendered string. Test more than 100 stably ordered results across at least two pages.

`--view=fcg` uses `GraphEdgeKind::Call` and `GraphScopeMode::Incident` around the seed symbol. Omitted `--depth` is 1-hop callers + callees. `--depth > 1` expands the frontier hop by hop. Each expansion step reuses the existing #18786 scoped-edge ordering, per-direction limits, and direction-specific truncation metadata. Multi-hop FCG frontiers inherit the 3.1 complete-frontier incoming-exclusion, DISTINCT public-edge `LIMIT`, eligible-refill, and already-emitted exclude contract — do not reimplement chunk membership or sentinel counting in `fcg.rs`. Pass the walk-global emitted-edge set into that refill so a prior-hop edge cannot consume the next hop's quota.

Keep one typed visited-node set and one emitted-edge set keyed by `(typed source, typed target, rel)` for the whole walk — the same uniqueness contract as 3.4. The seed starts visited. Each hop calls the 3.1 scoped reader on the current frontier only. A neighbor enters the next frontier only when its typed id is newly discovered. Expand each typed node at most once. Emit each public edge at most once. Stop when `--depth` is exhausted or the next frontier is empty. Top-level `incoming_truncated` / `outgoing_truncated` are the logical OR of those flags across fetches that actually ran. Do not invent a third walker. Never a project-wide CALLS scan. Never slice the rendered JSON or Mermaid after the walk.

Widen `match_clause(GraphEdgeKind::Call)` so scoped FCG matches the same target partitions the CALLS writer already projects: `CodeSymbol`, `ExternalSymbol`, and `UnresolvedCallee`. Carry stable node kind and typed ids through the view. Kind-aware standalone callees visibility (this section) is not enough for FCG — apply the 3.1 visible-owner set **before** every FCG `LIMIT` and hop expansion.

Mermaid: callers above the seed, callees below; edge label `CALLS`.

**Acceptance:**

- 3.2.1 - `gcode callees` returns outgoing CALLS through production `count_callees` / `find_callees` (not `find_callees_batch`). file: `crates/gcode/src/graph/code_graph/read/relationships.rs`.
- 3.2.2 - `--view=fcg` returns callers and callees with independent limits and truncation flags. symbol: `CodewikiFacts::scoped_edges`.
- 3.2.3 - Callees CLI matches callers pagination (`limit`/`offset`/`total`) and does not grow an output-clip token-budget flag. test: `crates/gcode/src/commands/graph/tests.rs::callees_mirrors_callers_pagination`.
- 3.2.4 - An asymmetric depth-2 fixture where only incoming truncates at one frontier and only outgoing truncates at another sets the two top-level flags independently and keeps the limited membership stable. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_or_aggregates_asymmetric_truncation`.
- 3.2.5 - More than 100 tied callees are returned in stable order across two pages. test: `crates/gcode/src/commands/graph/tests.rs::callees_paginates_past_max_graph_limit`.
- 3.2.6 - `Command::Callees` selects FalkorDB-only config next to `Command::Callers`. test: `crates/gcode/src/dispatch/tests.rs::graph_and_vector_commands_request_only_needed_services`.
- 3.2.7 - Local, external, and unresolved callee targets survive visibility; external/unresolved UUID ids are not required to exist in `code_symbols`. test: `crates/gcode/src/commands/graph/tests.rs::callees_keeps_external_and_unresolved_targets`.
- 3.2.8 - `--view=fcg` includes CodeSymbol, ExternalSymbol, and UnresolvedCallee CALLS targets within the declared bounds. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_includes_external_and_unresolved_targets`.
- 3.2.9 - Omitted `--depth` on `--view=fcg` walks one hop and serializes `depth: 1`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_omitted_depth_defaults_by_kind`.
- 3.2.10 - `gcode callees` parses as a top-level command with the same `limit`/`offset` shape as callers. test: `crates/gcode/src/cli/tests/top_level.rs::test_parse_callees_remains_top_level`.
- 3.2.11 - `--view=fcg` applies the 3.1 visibility-before-bound helper before every `LIMIT` and hop expansion. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_applies_visible_owner_set_before_limits`.
- 3.2.12 - A cyclic CALLS fixture at `--depth > 2` expands each typed node once, emits each public edge once, and stays deterministic. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_cycle_emits_unique_nodes_and_edges`.
- 3.2.13 - A depth-2 incoming walk whose prior-hop public edge sorts before a new neighbor still includes that neighbor at `incoming-limit=1` and does not set `incoming_truncated` unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_prior_edge_does_not_consume_next_hop_quota`.
- 3.2.14 - A depth-2 outgoing walk whose prior-hop public edge sorts before a new neighbor still includes that neighbor at `outgoing-limit=1` and does not set `outgoing_truncated` unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_prior_outgoing_edge_does_not_consume_next_hop_quota`.

### 3.3 Add MCG view with Leiden communities [category: code] (depends: 3.1, 3.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/mcg.rs`
- `crates/gcode/src/codewiki_facts/graph.rs::CodewikiFacts::resolve_scope_keys`
- `crates/gcode/src/codewiki_facts/graph.rs::CodewikiFacts::scoped_edges`
- `crates/gcode/src/codewiki_facts/graph_query.rs::*` — scope-reason: typed ScopeKeys plus the 3.1 Incident membership, DISTINCT-limit, eligible-refill, and already-emitted exclude contract for MCG hops
- `crates/gcode/src/index/import_resolution/context.rs::*` — scope-reason: expose pub(crate) candidate and invert helpers so MCG can resolve module→provider and provider→uniquely-resolving raw aliases from existing maps; no persisted provider column

`--view=mcg` seeds on a file path or module name. Use `GraphEdgeKind::Import` and `GraphScopeMode::Incident` so imports whose source is in scope are kept. Omitted `--depth` is 1-hop. Apply incoming/outgoing limits and the same OR-aggregated #18786 truncation flags as 3.2. Multi-hop expansion, when `--depth > 1`, reuses scoped-edge ordering and limits per frontier and inherits the 3.1 complete-frontier incoming-exclusion, DISTINCT public-edge `LIMIT`, eligible-refill, and already-emitted exclude contract — do not reimplement chunk membership or sentinel counting in `mcg.rs`. Pass the walk-global emitted-edge set into that refill so a prior-hop IMPORTS edge cannot consume the next hop's quota. Keep the same typed visited-node set and emitted-edge set as 3.2/3.4: expand each typed node at most once, emit each public `(typed source, typed target, rel)` once, and build the next frontier only from newly discovered typed nodes. Apply the 3.1 visible-owner set **before** every MCG `LIMIT` and hop expansion.

`ScopeKeys::Files` plus `endpoint_fields(Import)` currently compares one untyped value list to both `CodeFile.path` and `CodeModule.name`. Extend the scoped query contract with endpoint-typed file-path versus module-name frontier keys and carry those types through every MCG hop so a file path that equals a module name cannot admit incoming IMPORTS to the same-named module. Typed `file:` / `module:` prefixes on serialized output are not enough. Do not add a persisted ownership table.

Build an `AnalyticsGraph` from the **scoped** nodes and IMPORTS edges only, using the 3.1 typed canonical ids as `AnalyticsNode.id` / edge endpoints so Leiden cannot collapse a file path that equals a module name. Call `gobby_core::graph_analytics::analyze`. Write each node's Leiden community id onto `nodes[].community` and fill `communities` from `GraphAnalytics.communities`. Do not run Leiden on the whole project graph. Do not reimplement Leiden.

Reuse existing local import-target resolution; do not add a second resolver or a persisted module-ownership table. `CodeModule.name` is the module identity already projected from `ImportRelation.module_name`. Stored `IMPORTS` is importer `CodeFile` → imported `CodeModule` (`ADD_IMPORTS_CYPHER`). A unique incoming `CodeFile` is a unique consumer, not a provider.

Canonical seed identity:

1. A seed that matches exactly one `CodeFile.path` starts from that file key `P` and the equivalence class `E(P)` below.
2. A seed that matches exactly one `CodeModule.name` resolves provider file(s) through the existing `ImportResolutionContext` candidate-file helpers (`python_candidate_files`, `js_candidate_files`, `go_candidate_files`, `rust_import_candidate` / `rust_candidate_files`, and siblings). Intersect those candidates with active visible indexed files. Incoming `IMPORTS` / `code_imports.source_file` supply importer context for relative specifiers only — never owner identity. If exactly one provider file `P` remains, start from that file key plus the same `E(P)`.
3. A missing file or module seed is the same user error as a missing symbol seed in 3.1 — do not return an empty graph.
4. Zero or two-plus remaining provider files is a user error, not a guessed graph.

`E(X)` is the set of raw module names that uniquely resolve to provider file `X`. Compute it with one cached helper in `mcg.rs` (same operation for seed and every later frontier):

- Invert existing path/declaration helpers already owned by `ImportResolutionContext` (Python `python_module_names_for_path` and language siblings). Keep a derived name only when the existing candidate helpers, intersected with active visible files, uniquely yield `X`.
- Union every distinct `target_module` from the 2.2 active visible `(source_file, target_module)` reader whose existing helper resolution, using that row's `source_file` as importer context, uniquely yields `X`. Relative specifiers such as `.utils` or `./foo` appear only through this existing-fact pass.

This is identity closure against PostgreSQL `code_imports`, not an Incident walk and not a Falkor project-wide sample. Do **not** add a `code_imports` column, a module-ownership table, or an `IMPORTS` `provider_file` / target-file property.

Do **not** collapse a module seed to only its provider-file key before the Incident walk. Stored `IMPORTS` is `CodeFile.path` → `CodeModule.name`. A file-only frontier selects the provider's outgoing dependencies and cannot select consumers whose `IMPORTS` target is a raw module alias. Carry two typed frontier sets for both seed forms: file keys match only `source.path`, module keys match only `target.name`. Both the file-path seed `P` and every uniquely resolving module-name seed in `E(P)` start from `{file:P} ∪ {module:m | m ∈ E(P)}` and must yield the same nodes, edges, and directional flags.

After each hop, before the next scoped read, close every **newly discovered** typed node through that same `E` operation. A newly discovered uniquely resolved module `m` whose existing helpers yield exactly one provider file `Q` adds `file:Q` and every module in `E(Q)`. A newly discovered file `F` adds every module in `E(F)`. A missing or ambiguous provider stays a terminal module key: do not guess a file or alias set. Bounds, refill, already-emitted exclusion, and truncation still run on the closed frontier. Add a `pub(crate)` wrapper on `ImportResolutionContext` only if mcg.rs cannot see the existing candidate or invert helpers.

Mermaid: file/module nodes, `IMPORTS` edges, community id in the label.

**Acceptance:**

- 3.3.1 - MCG uses scoped IMPORTS edges, not a project-wide sample. symbol: `CodewikiFacts::scoped_edges`.
- 3.3.2 - Community ids come from `analyze` / Leiden, not a new partitioner. symbol: `analyze`.
- 3.3.3 - A two-cluster fixture yields two communities on the scoped subgraph. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_assigns_leiden_communities_on_scoped_imports`.
- 3.3.4 - A unique file path and a uniquely resolving module name whose provider is that file resolve to the same scoped node and edge set, including a consumer→provider→dependency fixture whose incoming consumers and outgoing dependencies appear for both seed forms. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_file_path_and_module_name_resolve_same_scope`.
- 3.3.5 - A missing module-name seed and a module with zero or two-plus provider files are user errors, not empty or guessed graphs. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_module_seed_rejects_missing_and_ambiguous`.
- 3.3.6 - Omitted `--depth` on `--view=mcg` walks one hop and serializes `depth: 1`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_omitted_depth_defaults_by_kind`.
- 3.3.7 - A file-path seed equal to a different module's name does not traverse that module's incoming IMPORTS; the walked node and edge set stays file-typed. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_file_seed_does_not_admit_same_named_module_imports`.
- 3.3.8 - `--view=mcg` applies the 3.1 visibility-before-bound helper before every `LIMIT` and hop expansion. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_applies_visible_owner_set_before_limits`.
- 3.3.9 - A module imported by exactly one consumer file whose provider is a different file canonicalizes to the provider, not the importer. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_module_seed_uses_provider_not_unique_importer`.
- 3.3.10 - A cyclic IMPORTS fixture at `--depth > 2` expands each typed node once, emits each public edge once, and stays deterministic. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_cycle_emits_unique_nodes_and_edges`.
- 3.3.11 - A depth-2 incoming IMPORTS walk whose prior-hop public edge sorts before a new neighbor still includes that neighbor at `incoming-limit=1` and does not set `incoming_truncated` unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_prior_edge_does_not_consume_next_hop_quota`.
- 3.3.12 - A depth-2 outgoing IMPORTS walk whose prior-hop public edge sorts before a new neighbor still includes that neighbor at `outgoing-limit=1` and does not set `outgoing_truncated` unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_prior_outgoing_edge_does_not_consume_next_hop_quota`.
- 3.3.13 - A depth-2 IMPORTS fixture where incoming truncates at one executed frontier and outgoing truncates at another keeps both top-level flags true and membership deterministic. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_or_aggregates_asymmetric_truncation`.
- 3.3.14 - A module seed and its unique provider-file seed both include consumers targeting the module name and the provider's outgoing IMPORTS; a file-only frontier is insufficient. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_module_and_provider_seeds_include_consumers_and_deps`.
- 3.3.15 - Two raw aliases that uniquely resolve to one provider (a path-derived name and an importer-relative name) plus the provider-file seed return identical nodes, edges, and directional flags; the file seed's module-key set is the full `E(P)`, not one guessed specifier. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_two_aliases_and_provider_file_share_equivalence_class`.
- 3.3.16 - A depth-2 chain `P → Q → R` where `P` imports an alias of `Q` and `Q` imports an alias of `R` yields the same second-hop nodes, edges, bounds, and direction flags from `P`'s file seed and every uniquely resolving alias of `P`; discovering the `Q` module closes through `Q`'s provider file and `E(Q)` so `R` is not omitted. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_depth_two_closes_discovered_frontier_equivalence`.

### 3.4 Add class-hierarchy view [category: code] (depends: 3.1, 2.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/class_hierarchy.rs`

`--view=class-hierarchy <class>` resolves the seed through the 3.1-exposed `resolve_symbol`. Do **not** use `CodewikiFacts::scoped_edges` as the complete-DAG reader: that API clamps at `MAX_DECLARED_EDGE_LIMIT` (10_000) and always emits `LIMIT limit+1`. Implement a dedicated hop walker in `crates/gcode/src/commands/graph/view/class_hierarchy.rs`:

- Stored orientation is derived → base. **Outgoing** from the seed = ancestors. **Incoming** = descendants.
- Keep two independent directed walks. The ancestor frontier follows **outgoing** `INHERITS|EXTENDS|IMPLEMENTS` only. The descendant frontier follows **incoming** only. Never fetch every incident edge for a frontier: that would let the walk enter a base and then follow an incoming edge to a sibling or cousin. Each directed hop paginates to exhaustion if it would exceed the existing 10_000 page size. Never drop in-scope edges. The page order must be a **total** order: `source.id`, `target.id`, `type(r)`, `id(r)`. Keyset continuation carries that whole tuple — `(source, target)` alone is not unique because the same endpoints can carry `EXTENDS` and `IMPLEMENTS`, and FalkorDB can store parallel relationships. Deduplicate the public edge set by `(source, target, rel)` after the walk. A unit test of the paginator must consume a fake hop larger than 10_000 with tied endpoints, mixed heritage types, parallel edges, and shuffled insertion. Prefer keyset over `OFFSET` so a tied page boundary cannot skip or duplicate. Apply the 3.1 visible-owner set **before** each page-exhaustion and hop-cut decision so invisible rows cannot consume a page or flip `incoming_truncated` / `outgoing_truncated`.
- Internal `RETURN source.id AS source, target.id AS target, type(r) AS rel, id(r) AS edge_id`. Use `edge_id` in the cursor predicate and next-cursor construction. Omit `edge_id` from the public JSON/Mermaid payload. JSON `edges[].rel` and Mermaid edge labels are `INHERITS`, `EXTENDS`, or `IMPLEMENTS` — not a coarse `Inheritance`.
- Match `CodeSymbol|ExternalSymbol|UnresolvedCallee` sources **and** targets (same partitions §2.1 writes). Every ancestor and descendant page query binds the resolved project on **both** endpoints (`{project: $project}` on each label partition). Identical symbol ids from another project must not enter the walk. External and unresolved nodes are included as terminals on either end: stable ids from the existing `make_external_symbol_id` / `make_unresolved_callee_id` helpers, labels from `name` / module, typed edges, no further expansion. A Trait-seeded descendant walk must return `impl LocalTrait for external_crate::T` as an incoming `IMPLEMENTS` edge from that external source. A non-`CodeSymbol` endpoint stays visible when the relationship-owning impl/derived file is visible.
- Walk with bounded visited node and edge sets so a shared ancestor is emitted once.
- `incoming_truncated` / `outgoing_truncated` are true only when `--depth` cut a further hop on that directed frontier. Omitted `--depth` is 8 (`1..=16`).
- Do not use `GraphScopeMode::Closed` with a single-class seed.

Mermaid: `flowchart TB`, bases above derived, opaque `n0`/`n1` node IDs from 3.1, edge labels `EXTENDS` / `IMPLEMENTS` / `INHERITS`.

Integration fixture (can live next to heritage parser tests or as a gcode graph unit test with a stub graph): a known `Base` / `Derived` pair produces the inheritance edge and appears in both the ancestor query of `Derived` and the descendant query of `Base`. A three-level `A <- B <- C` chain at depth 8 includes `A` when seeded at `C`, and depth 1 omits `A`. Seeded at `A`, depth 1 includes `B` and omits `C` with `incoming_truncated=true` and `outgoing_truncated=false`; at sufficient depth the full descendant chain is present and `incoming_truncated` is false. A diamond `D->B`, `D->C`, `B->A`, `C->A` (derived-to-base) seeded at `D` includes `A`, `B`, `C` and all four edges exactly once; seeded at `A` includes `B`, `C`, `D` exactly once. At depth 1 from `D`, `A` is absent and the ancestor-side flag is true; at the default depth, `A` is present and that flag is false. A base with two children `Left` and `Right`, seeded at `Left`, includes `Base` and does **not** include `Right`.

**Acceptance:**

- 3.4.1 - `--view=class-hierarchy` returns the ancestor/descendant DAG through the dedicated hop walker, not `scoped_edges`. file: `crates/gcode/src/commands/graph/view/class_hierarchy.rs`.
- 3.4.2 - A same-file base/derived pair appears in both directions: outgoing from Derived is Base (ancestor); incoming to Base includes Derived (descendant). test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_includes_base_and_derived_both_directions`.
- 3.4.3 - Depth 1 omits the grandparent; default depth includes it. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_depth_caps_chain`.
- 3.4.4 - A diamond DAG emits each shared ancestor and each edge once; depth 1 from `D` omits `A` and sets the ancestor-side hop-cut flag; default depth includes `A` and clears that flag. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_diamond_is_complete_dag`.
- 3.4.5 - JSON and Mermaid distinguish `INHERITS`, `EXTENDS`, and `IMPLEMENTS` via `type(r)`. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_preserves_heritage_subtypes`.
- 3.4.6 - External and unresolved heritage targets appear as terminal nodes with stable ids and typed edges. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_includes_external_and_unresolved_terminals`.
- 3.4.7 - A hop larger than `MAX_DECLARED_EDGE_LIMIT` is fully consumed by pagination; the result is not silently capped. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_paginates_hop_to_exhaustion`.
- 3.4.8 - Pagination uses a total order `(source.id, target.id, type(r), id(r))`; tied endpoints with different heritage types and parallel edges neither skip nor duplicate across page boundaries. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_pagination_is_total_order`.
- 3.4.9 - Seeded at one child of a two-child base, the sibling is absent from nodes and edges. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_excludes_siblings`.
- 3.4.10 - Omitted `--depth` on `--view=class-hierarchy` uses 8 and serializes `depth: 8`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_omitted_depth_defaults_by_kind`.
- 3.4.11 - A local Trait seed returns an external or unresolved impl Type as a descendant terminal with a typed `IMPLEMENTS` edge. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_includes_external_and_unresolved_sources`.
- 3.4.12 - `--view=class-hierarchy` applies the 3.1 visibility-before-bound helper before each page-exhaustion and hop-cut decision. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_applies_visible_owner_set_before_hop_cut`.
- 3.4.13 - A base-seeded descendant chain at depth 1 sets `incoming_truncated=true` and `outgoing_truncated=false`; at sufficient depth the full descendant chain is present and `incoming_truncated` is false. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_descendant_depth_caps_incoming`.
- 3.4.14 - Two edges that share source, target, and type but differ by `id(r)` paginate without skip or duplicate; `edge_id` is used internally and is absent from the public payload. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_pagination_uses_internal_edge_id`.
- 3.4.15 - Two projects that share endpoint ids do not contaminate a CHG ancestor or descendant page; both endpoints are project-bound. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_binds_project_on_both_endpoints`.

### 3.5 Update CLI contract and code-index skill [category: code] (depends: 3.2, 3.3, 3.4)
`kind: deliverable`

Targets:
- `crates/gcode/src/contract.rs::contract`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: add callees and graph view command entries
- `crates/gcode/tests/contract.rs::*` — scope-reason: graph_view_payload_keys_are_complete and writer contract named in acceptance
- `tests/contracts/gcode.contract.json::*` — scope-reason: vendored Python CLI-contract snapshot consumed by tests/test_cli_contracts.py
- `tests/test_cli_contracts.py::test_gcode_contract_covers_daemon_consumed_surface`
- `docs/contracts/gcode-cli.md::*` — scope-reason: public CLI contract lists callees and graph view
- `crates/gcode/assets/SKILL.md`
- `src/gobby/install/shared/skills/code-index/SKILL.md`
- `tests/skills/test_code_index_skill.py::*` — scope-reason: document the new commands and keep the two SKILL.md copies byte-identical
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh the code-index skill content hash only
- `crates/gcode/src/contract/graph_view.rs`
- `src/gobby/hooks/code_navigation.py::*` — scope-reason: register `callees` and nested `graph view` as read navigation without classifying `graph clear` / `rebuild` / `sync`
- `src/gobby/ai/_tool_chat_tools.py::*` — scope-reason: add `callees` to the read-only set and authorize exactly nested `graph view` under a no-mutation policy
- `tests/hooks/test_normalization.py::*` — scope-reason: existing `gcode callers` navigation case plus `callees` and `graph view` / mutating graph subcommands
- `tests/ai/test_tool_chat_tools.py::*` — scope-reason: existing mutator policy cases plus `callees` and nested `graph view` read-only / `graph clear` mutating assertions

This leaf is production Rust (`contract.rs` / `contract/graph_view.rs` / generated `gcode.contract.json`) plus the two SKILL.md copies. Route it as `category: code`, `task_type: feature`, `tdd: true`, `implementation_domain: backend`. Keep the documentation updates in the same leaf.

Add `callees` and `graph view` to `contract()` and regenerate / update `crates/gcode/contract/gcode.contract.json` the same way other commands are declared (`graph_read_flags`, view-specific flags, mermaid always present in the payload key list). Copy the same command entries into the vendored `tests/contracts/gcode.contract.json` snapshot that `tests/test_cli_contracts.py` loads. Advance/document the contract version in `docs/contracts/gcode-cli.md` so the public CLI contract names `callees` and `graph view`. Do not expand this leaf into README or gcode-user-guide rewrites. `graph_payload_keys` must include `project_id`, `project_root`, `view`, `seed`, `depth`, `incoming_truncated`, `outgoing_truncated`, `hint`, `nodes`, `edges`, `communities`, `mermaid`.

Register the new read surfaces in the hook and tool-chat inventories. Add `callees` to `GCODE_NAVIGATION_COMMANDS` and `GCODE_READONLY_TOOLS` next to `callers`. `gcode_navigation_metadata` must treat `gcode graph view` as a read navigation command and must not classify `gcode graph clear`, `rebuild`, `sync-file`, or `cleanup-orphans` as navigation. Tool-chat keeps top-level `graph` mutating in `GCODE_ALLOWED_TOOLS`. A no-mutation policy may authorize exactly the nested `graph view` argv (`graph` plus `view`) and must reject `graph clear` / `rebuild` / `sync-file` / `cleanup-orphans`. Do not add `graph` itself to `GCODE_READONLY_TOOLS`.

Before expansion, convert `#17680` from `task_type: feature` to `epic` (or bind another eligible open epic). After expansion, update root task `#17680` `description` and `validation_criteria` so they name `fcg`, `mcg`, `class-hierarchy`, `callees`, Leiden-on-mcg, complete CHG within `--depth`, and mandatory complete Mermaid. The written bar today is CHG-only. This leaf does not convert the type during review.

Document in both SKILL.md copies:

- `gcode callees <symbol>` (`limit`/`offset` only; no output-clip `--token-budget`)
- `gcode graph view --view=fcg|mcg|class-hierarchy <seed>`
- CHG is complete within `--depth` (no row LIMIT); omitted `--depth` is 8 for CHG and 1 for FCG/MCG
- FCG/MCG keep #18786 incoming/outgoing edge limits and report `incoming_truncated` / `outgoing_truncated`; they do not clip JSON or Mermaid
- MCG communities are Leiden via `analyze`
- A unique MCG file-path seed and every uniquely resolving raw module alias of that file (`E(P)`, including importer-relative specifiers recovered from active `code_imports`) yield the same scoped graph. Walk the provider-file key plus every module key in `E(P)` so consumers of each alias and outgoing dependencies of the provider appear for all those seeds. After each hop, close newly discovered files and uniquely resolved modules through the same `E` operation so a discovered alias of `Q` still reaches `Q`'s provider dependencies. Incoming IMPORTS are consumers, not owners. Do not persist a provider-file column or ownership fact.
- `nodes[].file` is nullable: declaring path for files/symbols, unique provider path for a uniquely resolved module, otherwise null.

Refresh the SHA-256 entry for the install-shared skill in `bundled_content_manifest.json`. Do not regenerate the whole manifest in a dirty worktree; hash only the changed skill blob.

`contract.rs` is 769 lines. If the new command entries would push it to 1,000, extract view/callees contract helpers into `crates/gcode/src/contract/graph_view.rs` in this same leaf.

**Acceptance:**

- 3.5.1 - CLI contract lists `callees` and `graph view` with `nodes`, `edges`, and the other payload keys above. symbol: `contract`.
- 3.5.2 - Both SKILL.md copies document the new commands. file: `crates/gcode/assets/SKILL.md`.
- 3.5.3 - Bundled manifest hash matches the install-shared skill. file: `src/gobby/install/bundled_content_manifest.json`.
- 3.5.4 - Generated contract tests assert the complete `graph_payload_keys` set including `project_id`, `project_root`, `hint`, `nodes`, and `edges`. test: `crates/gcode/tests/contract.rs::graph_view_payload_keys_are_complete`.
- 3.5.5 - After converting `#17680` to an epic and expanding, its description and `validation_criteria` name fcg, mcg, class-hierarchy, callees, Leiden-on-mcg, and complete Mermaid. behavior: "root task #17680 product bar matches this plan".
- 3.5.6 - Vendored `tests/contracts/gcode.contract.json` matches the crate contract for `callees` and `graph view`; `docs/contracts/gcode-cli.md` names both commands. file: `tests/contracts/gcode.contract.json`.
- 3.5.7 - `contract_version` advances from the current value (4 at this writing) by exactly one and is identical in `contract()`, `crates/gcode/contract/gcode.contract.json`, `tests/contracts/gcode.contract.json`, and `docs/contracts/gcode-cli.md`. symbol: `contract`.
- 3.5.8 - `gcode callees` and `gcode graph view` classify as read navigation; `gcode graph clear` does not. test: `tests/hooks/test_normalization.py::test_gcode_callees_and_graph_view_are_navigation`.
- 3.5.9 - A no-mutation tool-chat policy accepts `callees` and nested `graph view` and rejects `graph clear`. test: `tests/ai/test_tool_chat_tools.py::test_graph_view_is_readonly_without_graph_mutators`.
- 3.5.10 - The code-index skill tests document the new commands and still require the two SKILL.md copies to be byte-identical. test: `tests/skills/test_code_index_skill.py::test_code_index_skill_matches_gcode_bundled_asset_when_present`.

## E1 End-to-end verification
`kind: verification`

End-to-end bar for #17680 after all leaves:

- Indexer extracts `INHERITS` / `EXTENDS` / `IMPLEMENTS` for every explicit-heritage language listed in 1.2, including imported bases promoted through retryable LocalImport rows.
- `gcode graph view --view=class-hierarchy Derived` returns Base as an outgoing ancestor of Derived and Derived as an incoming descendant of Base, with typed `rel` values, complete JSON, and a complete valid Mermaid fence that uses opaque `n0`/`n1` node IDs.
- `gcode graph view --view=fcg` and `--view=mcg` work on existing CALLS / IMPORTS; MCG node communities come from Leiden; FCG/MCG OR-aggregate #18786 direction flags across hops.
- `gcode callees` is a first-class outgoing-1-hop read with caller-parity `limit`/`offset` and no output-clip token-budget trim.
- Successful views never character/token-slice the payload.
- Focused commands: `cargo test -p gobby-code` for the touched modules; `cargo test -p gobby-core graph_analytics`; `gdaemon schema verify` after 1.1; `GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py -v` after the 1.1 identity hop. Do not run the full pytest suite.

## V1 Plan Changelog
`kind: verification`

**Draft** `kind: verification`

- Decision Record confirmed. Full depth; expand under #17680; fcg + mcg + class-hierarchy + callees; Leiden via existing `analyze`; explicit heritage plus Rust impl/supertrait and Go embedding; complete CHG at depth 8 with no row LIMIT; FCG/MCG keep #18786 limits; Mermaid always emitted and validated.
- `#17679` timeout is already fixed; views stay datastore-bounded.

**Round 1** `kind: enhancement`

- enhancer_run: e21d6325-6a47-4743-8eda-2523fde79166
- enhancer_session: a4ff4e69-af52-466b-8ad9-725d469bbcd1
- converged: false
- suggestions_presented: 5
- accepted:
  - E1 / better / testability — shared renderer sort, escape, hostile-label matrix
  - E2 / better / clarity — OR-aggregate #18786 direction flags across FCG/MCG hops
  - E3 / better / testability — two-file LocalImport promotion plus rebuild of the promoted row
  - E4 / better / testability — diamond DAG uniqueness, visited set, hop-cut flags
  - E5 / better / clarity — slim: MCG file-path and module-name seeds resolve to the same scope
- declined: none
- resolution_notes: Folded E1–E4 as specified. Slimmed E5 to dual-seed identity and a 3.1 cross-reference for missing seed (no second missing-seed essay). Human Decision Record addendum: synthesis token budgets are allowed (generate under N tokens); output clipping is forbidden (`text.left(N)`, Graphify `--budget` string cut, `trim_results`). View/callees are deterministic dumps so they emit complete payloads or error; they do not grow an output-clip `--token-budget`. #18786 flags stay structured edge-limit / hop-cut metadata.

**Review attempt 3** `kind: verification`

- reviewer_run: df0ccb36-afda-4696-808e-5d9fbe415523
- reviewer_session: 71e0436f-44c1-427a-aab6-4184709b5072
- evidence_id: 4e68cbc5-0a6c-4b28-84ee-616215b13512
- verdict: protocol_failure (`shadow_manifest_mismatch`); no `coverage_attestation`; attempt does not count toward `completed_plan_review_rounds`
- findings_presented: R3-F01–R3-F11 (all blocking; salvage)
- accepted:
  - R3-F01 slim: independent `source_external_module` carrier; no new store
  - R3-F02 heritage deletes by `r.file` / `r.source_file_path`
  - R3-F03 slim: promotion dirties owning files into existing graph projection only
  - R3-F04 FCG `match_clause` includes ExternalSymbol and UnresolvedCallee
  - R3-F05 slim: document the three rels in existing `docs/contracts/shared-graph-schema.md`
  - R3-F06 optional `--depth` with per-view effective defaults (8 CHG, 1 FCG/MCG)
  - R3-F07 two directed CHG frontiers; sibling exclusion fixture
  - R3-F08 named provider-first target and source tests
  - R3-F09 slim: typed canonical ids (`symbol:` / `file:` / `module:`)
  - R3-F10 slim: reuse `visible_graph_paths` for every view edge; no new visibility store
- declined:
  - R3-F11 graph-generation / extra read lock — extra mechanism; existing callers/usages already read a live graph and `with_code_graph` is not a snapshot lock
- resolution_notes: Unattended ballot against compile/correctness/security-parity/production-path. Official V1 adversary fence was not written; evidence expired.

**Review attempt 2** `kind: verification`

- reviewer_run: 57f5f804-68e1-40d8-8601-c494f7272024
- reviewer_session: 4b36f3ea-8ae4-431b-8617-14352d137823
- evidence_id: e2d5849c-3b30-4219-9637-22d3c3af18a3
- verdict: protocol_failure (`duplicate_finding` on `R2-F01`); no `coverage_attestation`; attempt does not count toward `completed_plan_review_rounds`
- findings_presented: R2-F01–R2-F11 (all blocking; salvage)
- accepted:
  - R2-F01 corrected: next unused hop is 390 (389 already committed); remove 387; pin identity/grant/cli consumers
  - R2-F02 `code_inheritance` pkey + UNIQUE NULLS NOT DISTINCT + content-version FK
  - R2-F03 constructor compatibility in the introducing leaf (ParseResult, LanguageSpec, SyncFileMutation)
  - R2-F04 slim: cross-file Rust impl source via retryable LocalImport, no new store
  - R2-F05 slim: `db/queries.rs` readers mirroring the call pair
  - R2-F06 `projection/sync.rs::sync_graph_file` production path
  - R2-F07 slim: heritage-live orphan predicates on already-targeted cleanup
  - R2-F08 slim: existing CodeFile/CodeModule/IMPORTS canonicalization; no persisted ownership
  - R2-F09 callees dispatch, service selection, kind-aware visibility
  - R2-F10 CHG total-order pagination
  - R2-F11 slim: acceptance-named tests plus vendored contract + gcode-cli.md; no README/guide sweep
- declined: persisted module-ownership store; candidate-intersection query beyond the call analog; README/user-guide gold-plating
- resolution_notes: Unattended ballot. Catalog head moved to 389 during this session, so the reviewer's "use 389" fix was already stale. Official V1 adversary fence was not written; evidence expired.

**Review attempt 1** `kind: verification`

- reviewer_run: c152cadd-a9ca-4be7-8d56-f72c3efe33d7
- reviewer_session: 4aac6206-76d3-4787-be52-ca66bd2511da
- evidence_id: 29fd19fb-96d6-4f35-8769-d23fb434382d
- verdict: protocol_failure (`shadow_manifest_mismatch`); no `coverage_attestation`; attempt does not count toward `completed_plan_review_rounds`
- findings_presented: F01–F20 (all blocking)
- accepted:
  - F01 migration 388
  - F02 slim: policy inventory + tests, no hand-written CREATE POLICY
  - F03 retryable LocalImport
  - F04 sync_token
  - F05 parser/tests.rs mod heritage
  - F06 serial_db ParseResult
  - F07 GraphFileFacts pipeline
  - F08 commands/graph.rs mod view
  - F09 dedicated CHG walker / paginate to exhaustion
  - F10 type(r) subtypes
  - F11 outgoing=ancestors, incoming=descendants
  - F12 production count/find callees
  - F13 depth 1..=16
  - F14 reject CHG row-limit flags
  - F15 §3.5 category code
  - F16 nodes/edges in payload keys
  - F17 external/unresolved terminals
  - F18 #17680 acceptance
  - F19 opaque mermaid IDs
  - F20 slim: call-parity invalidation, no provenance store
- declined: none
- resolution_notes: Folded the approved ballot into the narrative. Official V1 adversary fence was not written because the reviewer returned protocol_failure; evidence is expired rather than finalized.

**Review attempt 4** coordinator ballot (unattended). Verdict `needs_review` with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted all eight findings, slimming extra mechanism: R4-F01 visibility before LIMIT/hop-cut via existing helpers (no new public triple type); R4-F02 thread graph_file_paths vs vector_file_paths through pipeline/overlay/IndexOutcome/request/run; R4-F03 CALLS-parity MERGE endpoints; R4-F04 MERGE key file+line+content_hash; R4-F05 typed file/module frontier keys during MCG walk; R4-F06 split view/ under the 1,000-line ceiling; R4-F07 expose resolve_symbol in §3.1; R4-F08 document LocalImport call-miss vs inheritance-miss. validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"bb79ce60-01f2-4d95-b89e-299a995c5b54","plan_hash":"5b2da75af04e9a43a52cc0183af1329bd300f7d1cd27cc7c06468580b0ef55dc","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ff6b9a64acfce374b6428254e6c4a8d8878a4e3d997de675d2541c8f2fbc0022","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":8,"total":12},"evidence_id":"bb79ce60-01f2-4d95-b89e-299a995c5b54","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"1a0285a3f12a098fbbee7a39df64bfeebe22ad9a4dbae21f1a8773f0666183f3","status":"valid"},"source_digest":"d9f0c96d16743e2daee0838ed6f53366d6f29a11fb19059d714bbc8fdd3d7052","version":1},"findings":[{"category":"unhandled-edge","check_key":"visibility-before-edge-bounds","description":"The plan says views may constrain or post-filter traversed edges. EdgeQueryPlan applies LIMIT limit+1 before returning only source/target, while visible_graph_paths loses content hash and project identity.","finding_id":"R4-F01","fix":"Apply existing visibility helpers before every FCG/MCG LIMIT and CHG page/hop-cut decision. Test invisible rows ordered first plus overlay cases.","location":"Phase 3 / §§3.1–3.4","prevention":"For every bounded graph read, verify active project, owner path, and content hash are applied before the limit sentinel and truncation decision.","principle":"Visibility and overlay precedence must be resolved before bounded datastore selection and truncation classification.","root_cause":"The plan permits post-filtering even though the current scoped query returns only endpoint pairs after LIMIT and the visibility helper returns bare paths.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"target-specific-projection-path-threading","description":"Promotion owners cannot reach the promised graph-only projection.","finding_id":"R4-F02","fix":"Thread separate graph_file_paths and vector_file_paths end to end.","location":"Phase 1 §1.3 / Phase 2 §2.2","prevention":"Trace every new return value through pipeline variants, outcome/request/status structs, command wrappers, and final backend calls.","principle":"A target-specific state set must be carried through every producer, wrapper, and terminal consumer that executes the state transition.","root_cause":"Section 1.3 names the graph-only union at attach_projection_sync but omits the callers and data shapes that discard promotion owners.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"order-independent-heritage-endpoints","description":"An owner-file sync before the newly indexed provider makes MATCH yield no row.","finding_id":"R4-F03","fix":"Use CALLS-parity MERGE for source and target CodeSymbol nodes.","location":"Phase 2 / §§2.1–2.2","prevention":"Compare every new relationship writer with the established writer for endpoint creation.","principle":"Projection writers must create relationship endpoints so valid file-sync order cannot silently suppress a persisted edge.","root_cause":"The concrete heritage Cypher uses MATCH for both CodeSymbol endpoints, while CALLS uses MERGE.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"heritage-relationship-identity-parity","description":"Two same-endpoint same-type inheritance facts collapse into one relationship.","finding_id":"R4-F04","fix":"Key every heritage relationship by file, line, and content_hash, matching CALLS.","location":"Phase 2 §2.1 / Phase 3 §3.4","prevention":"Compare uniqueness key with the graph relationship MERGE key.","principle":"Projection relationship identity must preserve the persisted fact key until the public read layer intentionally deduplicates it.","root_cause":"The proposed heritage MERGE keys a relationship only by content_hash.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"typed-identity-during-mcg-traversal","description":"An MCG file seed can admit incoming imports to the same-named module.","finding_id":"R4-F05","fix":"Extend the scoped query contract with endpoint-typed file-path and module-name frontier keys.","location":"Phase 3 / §§3.1 and 3.3","prevention":"Verify seed and frontier types in query predicates through every hop.","principle":"Typed node identity must constrain traversal itself.","root_cause":"ScopeKeys::Files compares one untyped value list to both CodeFile.path and CodeModule.name.","section_id":"3.3","severity":"blocking"},{"category":"gobby-format","check_key":"projected-view-module-line-ceiling","description":"The single planned file threatens the 1,000-line ceiling.","finding_id":"R4-F06","fix":"Replace the single file with commands/graph/view/ modules.","location":"Phase 3 / §§3.1–3.4","prevention":"Estimate projected line counts and assign independent mechanisms to bounded modules.","principle":"A plan must decompose a projected production monolith before expansion.","root_cause":"Sections 3.1–3.4 place too much in one new commands/graph/view.rs.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"shared-seed-resolver-before-view-leaves","description":"Section 3.4 cannot reuse callers symbol resolution from its declared dependencies.","finding_id":"R4-F07","fix":"Move resolver-sharing work into §3.1.","location":"Phase 3 / §§3.1, 3.2, and 3.4","prevention":"Check symbol visibility and dependency reachability from each consuming leaf.","principle":"A shared private seam must be exposed in the common predecessor of every leaf that consumes it.","root_cause":"Only §3.2 targets the private callers resolver, while CHG §3.4 depends on §3.1.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"shared-enum-state-documentation","description":"The current enum comment says LocalImport never persists after an index run.","finding_id":"R4-F08","fix":"Document call-miss versus inheritance-miss transitions.","location":"Phase 1 / §§1.1 and 1.3","prevention":"Audit enum documentation when reusing it with new lifecycle semantics.","principle":"Documentation on a shared state enum must describe each consumer's reachable transitions.","root_cause":"The plan reuses CallTargetKind::LocalImport with persistent inheritance misses but omits its transient-only documentation.","section_id":"1.1","severity":"nit"}],"reviewer_session":"18ef9a2a-21c5-4f79-b645-8cfc419385bb","round":4,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

**Review attempt 5** coordinator ballot (unattended). Verdict `needs_review` with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted all three findings, slimming extra mechanism: R5-F01 replace ProjectionSyncStatus.file_paths with graph_file_paths/vector_file_paths and copy them through pending_after_code_fact_write (no new public type); R5-F02 apply the existing four CallTargetKind tiers to Rust impl Type sources and reuse CALLS ExternalSymbol/UnresolvedCallee MERGE on either endpoint; R5-F03 extend the Kotlin/Swift INHERITS fallback to C# class base_list (interface=EXTENDS, struct=IMPLEMENTS, imported/unresolved class bases stay INHERITS; no record extraction; no promotion rewrite of heritage_kind). validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"68f5634d-45e2-4375-bb58-244dd61f33a4","plan_hash":"272f496846a51d4d6baae5d4c298814e1f6e706274a151eebfaa083909ea9902","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f7c38aec379bfaccd2df9daf578076c46601baaa8117eab059b2ca5f0a6676d5","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":3,"total":3},"evidence_id":"68f5634d-45e2-4375-bb58-244dd61f33a4","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"630dcdde280b9116084f46a8a698a22659a904a49381c39b4e8fa3ee4935cdaf","status":"valid"},"source_digest":"05b94e688f1edaee93301e1a75eb346abd3290b3bee7a6609555426f1524392a","version":1},"findings":[{"category":"traceability","check_key":"projection-status-target-path-split","description":"ProjectionSyncStatus still collapses graph-only promotion owners and vector-indexed files into one file_paths list. With graph_pending and vectors_pending both true, the public pending status cannot state that an owner-only path belongs only to graph projection, recreating the wrapper loss even if commands/index.rs executes the two lists correctly.","finding_id":"R5-F01","fix":"In §1.3, replace ProjectionSyncStatus.file_paths with graph_file_paths and vector_file_paths, or an equivalent typed per-target representation. Update pending_after_code_fact_write, IndexOutcome serialization/public re-exports, and snapshots; add a test where the promoted owner is graph-pending only while the newly indexed provider is pending for both targets.","location":"Phase 1 / §1.3","prevention":"Trace each target-specific collection through producers, execution requests, pending-status wrappers, public re-exports, serialization snapshots, and terminal consumers.","principle":"Target-specific file sets must remain target-specific through every request, status, outcome, and serialized wrapper.","root_cause":"The repaired graph_file_paths/vector_file_paths flow names execution carriers but omits ProjectionSyncStatus, whose single file_paths field is produced by pending_after_code_fact_write and serialized inside IndexOutcome.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"rust-source-endpoint-kind-coverage","description":"Legal explicit Rust heritage such as impl LocalTrait for external_crate::ExternalType, plus unresolved source types in partial code, produces no local source_symbol_id. The planned CodeSymbol-source-only writer and hierarchy query therefore cannot preserve or return those admitted impl facts.","finding_id":"R5-F02","fix":"Define the Rust impl source resolution tiers as Symbol, LocalImport, External, and Unresolved. Partition and MERGE ExternalSymbol/UnresolvedCallee source endpoints in §2.1, make cleanup and the shared graph schema symmetric in §2.2, allow non-CodeSymbol terminal sources in §3.4, and add end-to-end external/unresolved-source tests including a Trait-seeded descendant query.","location":"Phase 1 / §1.2; Phase 2 / §§2.1–2.2; Phase 3 / §3.4","prevention":"For asymmetric facts, enumerate the source_kind × target_kind matrix and verify extraction, persistence, promotion, graph labels, deletion/orphan handling, and traversal for every reachable cell.","principle":"Every endpoint kind admitted by extraction must have a complete persistence, projection, cleanup, and read path.","root_cause":"The model reuses every CallTargetKind for Rust impl sources, but extraction details only Symbol/LocalImport sources while the concrete heritage writers and CHG match require a CodeSymbol source.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ambiguous-heritage-kind-fallback","description":"For C# class D : B, I, tree-sitter supplies one base list; imported or unresolved B and I do not carry parse-time class/interface identity. The current rule forces a guessed EXTENDS/IMPLEMENTS label that persistence and CHG will preserve as false typed data.","finding_id":"R5-F03","fix":"Extend the ambiguity rule to C#: interface-declaration bases are EXTENDS; class/struct base-list targets use EXTENDS or IMPLEMENTS only when a resolved same-file symbol kind proves the distinction; otherwise emit INHERITS. Add imported and unresolved mixed-base fixtures and verify persisted and CHG relation values.","location":"Phase 1 / §1.2","prevention":"For each emitting grammar, inventory syntactic discriminators and test same-file, imported, external, and unresolved targets before assigning EXTENDS or IMPLEMENTS.","principle":"A parser must use a conservative relation kind whenever syntax and resolved local facts cannot prove a semantic subtype.","root_cause":"The plan provides the INHERITS fallback only for Kotlin and Swift even though C# exposes class and struct bases through an undifferentiated base_list for imported or unresolved targets.","section_id":"1.2","severity":"blocking"}],"reviewer_session":"ac7171a1-aa2f-406f-84df-1bfd76171436","round":5,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

**Review attempt 6** coordinator ballot (unattended). Verdict `needs_review` with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted R6-F01–R6-F09 and R6-F11 slim. Declined R6-F10 (new owner-identity triple and Cypher owner predicate — extra mechanism; re-litigates R3-F10/R4-F01; callers already use the live graph plus path-visible helpers). Repairs: hop 391; heritage_kind CHECK acceptance; 1.1.8 carriers only; Ruby mixin / inherent-impl / empty-query fixtures; api_tests and top_level file-wide targets; stale-token delete moved into 2.2.6; INHERITS weight 2.5 acceptance; 3.1.9/3.1.10 helper-level; per-view apply items; callees parse vs dispatch split; MCG provider-file resolution via ImportResolutionContext, not unique importer. validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"d25773c1-9ee9-4c90-a454-ff1b87d92969","plan_hash":"b2035d00eabaecd69b68563594b7ab09feaa77e388acf62a9e805c910ac24604","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"d96cd71d6e26a38b9a297e5dec2f02669bcb0f175bd4ddeed8c74566b2b9e6fa","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":11,"total":18},"evidence_id":"d25773c1-9ee9-4c90-a454-ff1b87d92969","lanes":[{"candidate_count":9,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"ce0c3f64d8c0ff0803a2bc169e5cc422c9f4d63844c1bba2a0ea2fa7a1a3a35f","status":"valid"},"source_digest":"4390526d4448d51aa0a47cc6e85b28180a1bc617addb7c36a7d7d6dafd8446f3","version":1},"findings":[{"category":"traceability","check_key":"next-unused-schema-migration-hop","description":"Section 1.1 targets `390_code_inheritance.sql`, while `390_retain_interactive_credential_material.sql` is already registered and the expected latest schema version is 390. The planned filename, registration, and acceptance references collide with live repository state.","finding_id":"R6-F01","fix":"Change every section 1.1 target, registration reference, acceptance citation, catalog entry, baseline expectation, and identity pin to the next unused hop, currently `391_code_inheritance.sql`; retain an implementation-start `max(existing)+1` check.","location":"P1 / § 1.1","prevention":"Inspect migration files, the MIGRATIONS registry, catalog manifest, and expected schema identity together immediately before each review.","principle":"A migration deliverable must target the repository's current max-plus-one hop across every registry and identity pin.","root_cause":"The plan still names migration 390 after the repository registered an unrelated migration 390.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"schema-domain-constraint-acceptance","description":"The required `heritage_kind IN ('INHERITS','EXTENDS','IMPLEMENTS')` CHECK can be omitted while all current section 1.1 acceptance items pass.","finding_id":"R6-F02","fix":"Add a distinct section 1.1 acceptance item and focused migration/baseline schema test proving the CHECK exists and rejects every value outside the three allowed kinds.","location":"P1 / § 1.1","prevention":"Compare every named table constraint in a deliverable body against its acceptance items and focused schema tests.","principle":"Every required database domain invariant needs an acceptance item that proves both presence and rejection behavior.","root_cause":"The body requires a `heritage_kind` CHECK, while acceptance enumerates other constraints and omits this one.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"leaf-acceptance-owned-by-leaf","description":"Section 1.1 owns models and schema, yet acceptance 1.1.8 requires source-side promotion and miss persistence implemented only in section 1.3.","finding_id":"R6-F03","fix":"Restrict 1.1.8 to independent endpoint carrier representation and schema round-trip. Keep miss retention and one-side-only promotion exclusively under section 1.3 acceptance.","location":"P1 / § 1.1 and § 1.3","prevention":"For each acceptance item, identify the exact owning target and ensure it belongs to the same leaf or a predecessor.","principle":"A leaf's acceptance criteria must be satisfiable from that leaf's targets and predecessors.","root_cause":"Acceptance 1.1.8 includes miss persistence and promotion behavior owned by successor section 1.3.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"language-matrix-negative-branches","description":"Acceptance omits Ruby `include`/`extend`/`prepend` emitting `IMPLEMENTS`, inherent Rust `impl Type` emitting no heritage row, and the complete set of empty-query languages emitting no rows.","finding_id":"R6-F04","fix":"Add explicit acceptance items and fixtures for all three Ruby mixins, inherent Rust impl suppression, and every language in the empty-query set.","location":"P1 / § 1.2","prevention":"Enumerate every matrix row and every suppression branch, then map each to a named fixture or test.","principle":"A language behavior matrix needs explicit acceptance for each distinct positive and negative extraction branch.","root_cause":"Generic same-file pair coverage was used as a proxy for Ruby mixins, Rust suppression, and empty-query languages.","section_id":"1.2","severity":"blocking"},{"category":"gobby-format","check_key":"target-acceptance-symbol-parity","description":"`api_upsert_inheritance_reports_rows_inserted_not_input_len` is required by acceptance 1.3.3 and absent from section 1.3 Targets.","finding_id":"R6-F05","fix":"Add the exact new inheritance test symbol to Targets, or replace the narrow existing-test target with a justified `crates/gcode/src/index/api_tests.rs::*` target covering both changes.","location":"P1 / § 1.3","prevention":"Diff all `test:` acceptance references against exact test targets before review completion.","principle":"Every acceptance-owned new test symbol must appear in the deliverable's exact Targets block or a justified file-wide target.","root_cause":"Section 1.3 targets only the existing imports/calls inserted-row-count test while acceptance 1.3.3 names a new inheritance test.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"stale-delete-leaf-ownership","description":"Section 2.1 cannot independently satisfy rebuild and stale-token deletion because the deletion implementation and target file belong to section 2.2.","finding_id":"R6-F06","fix":"Move 2.1.4 into section 2.2 and merge it with overlapping acceptance 2.2.6. Keep sync-token production and edge creation in section 2.1.","location":"P2 / § 2.1 and § 2.2","prevention":"Map each state-transition acceptance item to its writer and cleanup targets before assigning phase dependencies.","principle":"An acceptance item must live with the leaf that owns its implementation seam.","root_cause":"Acceptance 2.1.4 requires stale-token deletion from `deletion.rs`, which is assigned only to successor section 2.2.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","check_key":"inherits-weight-acceptance","description":"`weight_for_kind` can leave `INHERITS` at the unknown default 1.0 while every current section 2.1 acceptance item passes.","finding_id":"R6-F07","fix":"Add an acceptance item tied to the existing alias test that asserts case-insensitive `INHERITS` and `inherits` map to 2.5 alongside `EXTENDS` and `IMPLEMENTS`.","location":"P2 / § 2.1","prevention":"Cross-check each relationship kind against writer, reader, and analytics-weight acceptance.","principle":"Every required analytics weight must have an exact acceptance assertion because fallback weights remain valid-looking outputs.","root_cause":"The body requires `INHERITS` weight 2.5, while acceptance covers graph writes and omits the analytics mapping.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"scaffold-successor-acceptance","description":"Section 3.1 explicitly allows FCG, MCG, and CHG handlers to remain stubbed, while its acceptance requires their traversal, visibility, bounds, and hop-cut behavior.","finding_id":"R6-F08","fix":"Limit section 3.1 acceptance to shared renderer, typed IDs, CLI validation, resolver exposure, and the lower-level visibility-before-bound contract. Move FCG, MCG, and CHG assertions to sections 3.2, 3.3, and 3.4, with the cross-view matrix in E1.","location":"P3 / § 3.1 through § 3.4","prevention":"Separate helper-level acceptance from per-view acceptance and place end-to-end matrices after all view leaves.","principle":"A common-scaffold leaf cannot require completed behavior from successor leaves that are allowed to remain stubs.","root_cause":"Acceptance 3.1.9 and 3.1.10 require all three live views even though sections 3.2 through 3.4 implement them.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"callees-parse-target-acceptance-parity","description":"`test_parse_callees_remains_top_level` has neither an exact Target nor a dedicated acceptance citation; acceptance 3.2.6 conflates parsing with service selection.","finding_id":"R6-F09","fix":"Target `crates/gcode/src/cli/tests/top_level.rs::test_parse_callees_remains_top_level` and split 3.2.6 into distinct top-level parsing and dispatch service-selection criteria.","location":"P3 / § 3.2","prevention":"Check CLI command additions across parser targets, dispatch targets, and separate acceptance items.","principle":"A new CLI parse regression needs its own exact target and acceptance citation.","root_cause":"The plan names the existing callers parse test and a dispatch test while requiring a new callees top-level parse test.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"active-owner-identity-before-bounds","description":"`visible_graph_paths` cannot distinguish active and stale relationships owned by the same path, and `EdgeQueryPlan` pins queries to one project. Stale or visible parent-project rows can therefore be excluded incorrectly or consume limits and hop-cut decisions across FCG, MCG, and CHG.","finding_id":"R6-F10","fix":"Specify a private active-owner identity `(project_id, file_path, content_hash)`, query every visible project ID, bind relationship provenance, and apply the owner predicate in Cypher before `ORDER BY`, `LIMIT`, page exhaustion, and hop-cut classification. Add same-path stale-hash and overlay-parent regressions for each view.","location":"P3 / § 3.1 through § 3.4","prevention":"Trace visibility identity through every query stage and test same-path stale content plus parent-project overlay owners before each bound.","principle":"Visibility predicates must use complete owner identity and run before ordering, limits, pagination exhaustion, and truncation classification.","root_cause":"The planned seam relies on path-only visibility and single-project queries, losing content-version and overlay-parent ownership.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"imports-direction-provider-resolution","description":"A `CodeModule` with exactly one incoming `CodeFile` has one importer, not one defining file. Mapping that module to the importer cannot guarantee equivalence between module-name and provider file-path seeds.","finding_id":"R6-F11","fix":"Replace incoming-edge ownership inference with the existing language-aware active local-import provider resolution and reject zero or multiple providers. If provider identity cannot be reconstructed reliably, persist an explicit resolved target-file fact and use it for MCG seed canonicalization.","location":"P3 / § 3.3","prevention":"Verify relationship direction at the writer and reader before deriving ownership or equivalence rules.","principle":"Seed canonicalization must follow stored edge semantics and identify the provider rather than an arbitrary consumer.","root_cause":"The plan treats a module's unique incoming `CodeFile` as its owner, while `IMPORTS` is stored from importer file to imported module.","section_id":"3.3","severity":"blocking"}],"reviewer_session":"0a8e3d4a-e127-471e-b11f-f738085e391e","round":6,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

Review attempt 7 coordinator ballot (unattended). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted all seven findings slim: F01 durable graph_synced=false and null attempted_at on promotion (not mark_graph_sync_attempted cooloff); F02 complete-frontier Incident incoming membership in the shared planner plus one >64-key test (FCG/MCG inherit); F03 1.1.11 plus focused golden-vector pytest in E1; F04 External/Unresolved target parser fixtures; F05 index.rs file-wide target; F06 base-seeded descendant hop-cut; F07 contract_version advances from 4 by one with cross-artifact equality. Declined none. validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"a6ab1c9a-b285-463f-8e3f-6fcc2bffa56c","plan_hash":"d2d1478c80980147925f52b611f2d025c050da1553421a404d6425b71d051771","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6390be2da1647571a0d5703fc72cd18c4f88fffbb540e0a9333b51d4caddc406","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":7,"total":10},"evidence_id":"a6ab1c9a-b285-463f-8e3f-6fcc2bffa56c","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"18222c9bc3b3c45b36d1c83a911a457364e9a506823250f48983bd9f08169d19","status":"valid"},"source_digest":"aa5373f25f1e715bb5ed00dc64850aae8be03bf74c70f14154aa86a08dda73eb","version":1},"findings":[{"category":"unhandled-edge","check_key":"promotion-owner-durable-graph-pending","description":"A provider-later promotion can change a row owned by a previously indexed file while that owner remains recorded as graph-synchronized. With sync_projections=false, no status or sync attempt dirties the owner, so later pending-recovery scans can omit the new typed edge.","finding_id":"R7-F01","fix":"In the same database operation that promotes either inheritance endpoint, set graph_synced=false for each owning active (project_id, file_path, content_hash) row without changing vectors_synced. Add ordinary and overlay provider-later tests proving disabled immediate sync leaves the owner recoverably graph-pending and enabled sync projects only the owner to graph.","location":"Phase 1 / §1.3; Phase 2 / §2.2","prevention":"Exercise provider-later promotion with immediate projection enabled and disabled on ordinary and overlay index paths; assert graph-pending and vector-pending state separately.","principle":"A persisted fact mutation must durably dirty every affected projection independently of whether immediate synchronization is enabled.","root_cause":"The plan carries promoted owner paths only through IndexOutcome and ProjectionSyncRequest; attach_projection_sync returns early when sync_projections is false, leaving the owner's existing graph_synced state unchanged.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"incident-frontier-global-membership","description":"FCG and MCG frontiers larger than 64 keys can count an internal frontier edge as incoming when its target is in one chunk and its source is in another. That row can consume the incoming limit and set incoming_truncated even though it should have been excluded.","finding_id":"R7-F02","fix":"Preserve complete typed-frontier membership for incoming exclusion while chunking only bounded query transport, then compute direction sentinels after globally merging eligible rows. Add depth-2 FCG and MCG regressions above 64 frontier nodes that assert stable membership, independent limits, and OR-aggregated truncation flags.","location":"Phase 3 / §§3.1–3.3","prevention":"Test every chunked bounded traversal above SCOPE_CHUNK_LEN with cross-chunk internal edges ordered before genuine external edges, checking membership and both direction flags.","principle":"Scope membership and truncation sentinels must be computed against the complete logical frontier before transport chunking.","root_cause":"Incoming Incident plans exclude sources with NOT source IN only the current 64-key chunk; an internal edge crossing two chunks is therefore misclassified before limits and later deduplication.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"runtime-grant-golden-validation","description":"The four signed runtime-grant golden files can be updated incompletely while the listed Rust schema checks still pass. Acceptance 1.1.6 names other identity consumers but does not own the Python golden-vector validator.","finding_id":"R7-F03","fix":"Add a distinct 1.1 acceptance item citing tests/runtime_grants/test_golden_vectors.py and add `GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py -v` to E1.","location":"Phase 1 / §1.1","prevention":"For every schema-identity hop, pair each golden rewrite recipe with its focused signature/checksum validation command.","principle":"Every regenerated signed artifact needs an acceptance-owned validator that checks its checksum and signature.","root_cause":"Section 1.1 requires regenerating four runtime-grant goldens, while its acceptance and E1 commands omit the focused golden-vector test that verifies recomputation and re-signing.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"heritage-terminal-target-tier-coverage","description":"External and Unresolved base targets are required by the four-tier heritage contract, yet the parser suite has no focused fixture proving target_kind, target_name, and target_external_module. Later projection/view tests can pass with synthetic rows while extraction remains wrong.","finding_id":"R7-F04","fix":"Add a named heritage parser test and acceptance item for External and Unresolved base targets, asserting target_kind, target_name, and target_external_module according to the existing call-resolution tiers.","location":"Phase 1 / §1.2","prevention":"For each endpoint, enumerate Symbol, LocalImport, External, and Unresolved and require parser fixtures for every reachable tier.","principle":"Every resolution tier admitted by extraction must be verified at the parser boundary where the tier is assigned.","root_cause":"Parser acceptance covers same-file and LocalImport target behavior plus Rust source terminals, but never materializes an External or Unresolved heritage target.","section_id":"1.2","severity":"blocking"},{"category":"gobby-format","check_key":"acceptance-test-target-parity","description":"The planned index_promotion_projects_owner_on_graph_only regression is not covered by the exact run target, so the expanded leaf's declared scope does not authorize all acceptance work.","finding_id":"R7-F05","fix":"Replace the exact commands/index.rs::run target with `crates/gcode/src/commands/index.rs::*` and a scope-reason covering both run-path wiring and the focused provider-later projection regression.","location":"Phase 1 / §1.3","prevention":"Cross-check every acceptance test artifact against Targets; use one justified ::* target when a leaf changes production and test symbols in the same existing file.","principle":"Every acceptance-owned changed test must fall inside the deliverable's declared exact or justified file-wide Targets.","root_cause":"The deliverable targets crates/gcode/src/commands/index.rs::run, while acceptance 1.3.12 creates a separate in-file test symbol outside that exact target.","section_id":"1.3","severity":"blocking"},{"category":"weak-testability","check_key":"chg-descendant-hop-cut-flag","description":"A swapped or coupled descendant hop-cut flag can pass the existing diamond and bidirectional-edge tests because only the ancestor-side truncation mapping is asserted.","finding_id":"R7-F06","fix":"Add a base-seeded descendant-chain acceptance test: at depth 1 assert incoming_truncated=true and outgoing_truncated=false; at sufficient depth assert the full descendant chain is present and incoming_truncated clears.","location":"Phase 3 / §3.4","prevention":"For every bidirectional traversal, test a depth cut and a fully consumed walk from both endpoint orientations, asserting the opposite flag stays false.","principle":"Independent directed frontiers require independent boundary tests for their mapped outcome flags.","root_cause":"The CHG acceptance suite tests the ancestor-side outgoing hop cut but never depth-caps a base-seeded descendant walk and checks incoming_truncated separately.","section_id":"3.4","severity":"blocking"},{"category":"weak-testability","check_key":"cli-contract-version-advance","description":"The new commands can appear in both JSON snapshots and docs while contract_version remains unchanged, satisfying the current parity criteria despite violating the stated version-advance requirement.","finding_id":"R7-F07","fix":"Add a 3.5 acceptance item asserting contract_version advances from the current value and is identical in contract(), crates/gcode/contract/gcode.contract.json, tests/contracts/gcode.contract.json, and docs/contracts/gcode-cli.md.","location":"Phase 3 / §3.5","prevention":"Whenever a public contract changes, assert the previous and next version plus equality across generator, snapshots, and documentation.","principle":"A required public contract-version change must be asserted explicitly, beyond snapshot parity.","root_cause":"The section requires advancing the contract version, while acceptance checks only command content and agreement between generated and vendored artifacts; all copies can agree on the old version.","section_id":"3.5","severity":"blocking"}],"reviewer_session":"b1f072a7-1962-4e6c-8434-16087ea6ff41","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

Review attempt 8 coordinator ballot (unattended). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted all three findings slim: F01 refill each incoming Incident chunk after complete-frontier HashSet filtering until limit+1 distinct eligible rows (strengthen 3.1.13 with saturation); F02 reuse callers RETURN DISTINCT so LIMIT counts public pairs, plus the same refill so parallel CALLS/IMPORTS cannot spend the sentinel (3.1.14 shared, FCG/MCG inherit); F03 inherit 3.4 visited-node and emitted-edge uniqueness for multi-hop FCG/MCG (3.2.12, 3.3.10). Declined none. validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"f149b764-6f2f-4385-be5a-6bc582587b10","plan_hash":"0ce957a316b6aafe1992313e3b62f0e04d6e7d7c07634899358feb94507a9d2c","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f67781dc0479081a071fb94a0993f2ce09ff65946884e1dfc9b02a7ca3343cfd","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":3,"total":3},"evidence_id":"f149b764-6f2f-4385-be5a-6bc582587b10","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"1ca8e68d0c16f5d8de141272ba605e2b4b6459c1e7eb6be2bc135c71d2d57324","status":"valid"},"source_digest":"80df1e43a6870a6562cf95e50aaa8240082e0a7d34a558b9d6d34a12773a4c5f","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"R7-F02","causal_section_ids":["3.1","3.2","3.3"],"check_key":"incident-full-frontier-filter-before-limit","description":"Incident incoming can fetch limit+1 rows for a target chunk and only then drop sources found in the complete frontier. More than limit+1 cross-chunk internal edges can therefore fill the chunk, hide genuine external incoming edges, and misreport truncation. The single cross-chunk edge in acceptance 3.1.13 does not exercise saturation.","finding_id":"R8-F01","fix":"Revise §3.1 so the existing scoped reader paginates/refills each incoming chunk after complete-frontier HashSet filtering until it has limit+1 eligible rows or exhausts the query, then classify truncation after the eligible global merge and deduplication. Strengthen 3.1.13 with more than incoming_limit+1 internally excluded rows ordered before genuine external rows; require FCG and MCG to inherit that shared regression.","introduced_in_round":7,"location":"Phase 3 / §§3.1–3.3","prevention":"For every bounded query, test more than limit+1 ineligible rows ordered before eligible rows and assert eligible membership plus truncation semantics.","principle":"Every eligibility predicate that defines a bounded result set must run before the limit sentinel, or the reader must refill after filtering.","root_cause":"The round-7 complete-frontier repair keeps full membership in a parent HashSet while allowing cross-chunk source exclusion after a chunk-local LIMIT limit+1 query.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"distinct-public-edges-before-limit","description":"Multiple CALLS facts for the same symbols can consume the entire sentinel window before endpoint-pair deduplication, starving later distinct FCG neighbors and setting a truncation flag even when fewer than limit distinct public edges survive. MCG shares the same scoped query contract.","finding_id":"R8-F02","fix":"Require the shared scoped query to deduplicate the public source/target/relationship identity before ORDER BY and LIMIT, retaining the global cross-chunk merge and deduplication. Add FCG and MCG regressions with more than limit+1 raw relationships for one public pair ordered before other distinct pairs, asserting complete distinct membership and truncation based only on distinct eligible overflow.","location":"Phase 3 / §§3.1–3.3","prevention":"Exercise every bounded edge reader with more than limit+1 parallel storage relationships for one public edge ordered before additional distinct edges.","principle":"A limit on public graph edges must count distinct public edge identities, never raw storage relationships that collapse afterward.","root_cause":"The shared scoped query applies LIMIT limit+1 to raw Falkor relationships, while CodewikiFacts::scoped_edges deduplicates public (source, target, rel) edges only after the bounded fetch.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"cycle-safe-multihop-frontier","description":"Cyclic CALLS and IMPORTS graphs can repeatedly re-expand the same typed nodes through depth 16 and accumulate duplicate nodes or edges. Renderer sorting and Leiden preparation do not define the traversal state transition or guarantee unique payload membership.","finding_id":"R8-F03","fix":"Specify one global typed visited-node set and one emitted-edge set keyed by (typed source, typed target, rel) for both FCG and MCG; build each next frontier only from newly discovered typed nodes, expand each typed node at most once, and stop when no new nodes remain while preserving OR flags from actual fetches. Add cyclic FCG and bipartite cyclic MCG regressions proving unique deterministic payloads and no repeated frontier expansion.","location":"Phase 3 / §§3.2–3.3","prevention":"Include cyclic depth-greater-than-two fixtures for every multi-hop view and assert each typed node is expanded once and each public edge is emitted once.","principle":"Bounded multi-hop graph walks require explicit global traversal state so cycles cannot re-expand nodes or duplicate payload membership.","root_cause":"Sections 3.2 and 3.3 define hop-by-hop expansion and OR-aggregated truncation without defining a global typed visited-node set, emitted-edge set, or newly discovered frontier rule.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"d3d0a5ce-6b1c-4c1e-9d27-f747c1b4593d","round":8,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

Review attempt 9 coordinator ballot (unattended). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted all three findings slim: F01 close the §3.1/§1.3 visibility ban to public or private triple types (helpers-before-LIMIT already specified; no new type); F02 one shared 3.1.15 final-output regression above the 10,000-character historical clip through the shared formatter; F03 pass the walk-global emitted-edge set into the 3.1 refill so already-emitted pairs are ineligible before quota (3.1.16, 3.2.13, 3.3.11). Declined none. validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"f00d22a5-e70c-4e2c-b0b6-02d6040a79ab","plan_hash":"cf47a585dec3cd90b59e5569ddbf31ffb3c1033a7faf75784570c3ce03d03665","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4431cccdb26514e48f40ac1ad9072fa0fabd9bc0163a80470079ee36cf67bd76","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":3,"total":3},"evidence_id":"f00d22a5-e70c-4e2c-b0b6-02d6040a79ab","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"bab127d886d20b0b24ebcb00115789d51b6afb3601659ed03e1a9b3e5d12762b","status":"valid"},"source_digest":"512008e35d65674bb508d307f9b2e59a06bd9becf1dd5469daa188dc85afe2c6","version":1},"findings":[{"category":"missing-requirement","check_key":"visibility-type-scope-parity","description":"The governing scope forbids any new public or private `(project_id, file_path, content_hash)` visibility type. Section 3.1 forbids only a new public type, so the FCG, MCG, or CHG implementation can still introduce the prohibited private mechanism.","finding_id":"R9-F01","fix":"State that no public or private visibility triple type may be added. Require the existing visibility helpers to run before every FCG/MCG `LIMIT` and every CHG page-exhaustion or hop-cut decision, with §§3.2–3.4 inheriting that seam.","location":"P3 / §3.1 through §3.4","prevention":"Diff every governing no-new-mechanism constraint against the owning deliverable and all inheriting leaves before review completion.","principle":"Every explicit scope prohibition must survive unchanged into the implementing deliverable.","root_cause":"Section 3.1 narrows the governing ban on public and private visibility-owner triple types to public types only.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"large-output-no-clipping-regression","description":"A post-render character or token slice can satisfy the current renderer-validity and per-view tests because none exercises a sufficiently large final payload. The central complete-JSON and complete-Mermaid requirement therefore lacks a regression that would detect clipping.","finding_id":"R9-F02","fix":"Add one shared final-output test above the historical clipping threshold for FCG, MCG, and class-hierarchy. Assert JSON parses, retains every expected node and edge, contains the complete Mermaid fence, and the extracted fence passes `is_valid_mermaid`.","location":"P3 / §3.1 and E1","prevention":"For every no-clipping requirement, test a payload larger than the historical clipping threshold through the final formatter and assert complete structured membership.","principle":"A mandatory complete-output contract needs a threshold-crossing final-boundary regression.","root_cause":"Current acceptance verifies small renderer payloads and pagination while never carrying a large rendered view through the final JSON/Mermaid output boundary.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"multihop-emitted-edge-before-limit","description":"After `A -> B` discovers `B`, the next frontier's incoming read can return `A -> B` again. With a finite incoming limit, that already-emitted edge can consume the sentinel before global deduplication, omit a new `C -> B` neighbor, and set `incoming_truncated` even though one new edge fits. The same transition exists for cyclic IMPORTS walks.","finding_id":"R9-F03","fix":"Pass the walk-global emitted-edge set into the shared continuation/refill path, treat already-emitted typed public edges as ineligible before each FCG/MCG hop quota, and refill to `limit+1` new eligible edges or exhaustion. Add bounded depth-2 FCG and MCG regressions with the prior edge sorted before the new neighbor.","location":"P3 / §3.2 and §3.3","prevention":"Exercise every bounded multi-hop walker with a prior-hop edge ordered before a new neighbor and assert the quota and truncation flag count only new eligible public edges.","principle":"A bounded walk over a globally unique public-edge set must exclude already-emitted edges before applying the next sentinel.","root_cause":"FCG and MCG deduplicate against the walk-global emitted-edge set only after each bounded scoped read.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"85dbfd88-5c8a-4971-a894-44c771dc7aa5","round":9,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

Review attempt 10 coordinator ballot (unattended, review cap reached). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. This is the last capped round: do not spawn another adversary. Human review required before expansion or build. Accepted slim: F01 1.1.8 structural carriers only; F02 privilege exact-set plus 1.3 source_inventory; F03 content-GC has_graph_facts includes code_inheritance; F04 gcode_postgres_objects inventory; F05 stale_cleanup and api_contract cleanup/retention; F06 active-content join on pending/promotion via existing file-state helpers; F09 CHG internal edge_id cursor; F10 mark_graph_synced CAS on captured hash; F11 outgoing exclude-and-refill tests. Declined: F07 cross-project overlay composition contract (extra mechanism; visible_graph_paths already composes overlay plus unshadowed parent); F08 owner-identity triple through internal graph rows (re-litigates R6-F10 / R4-F01 / R9-F01). validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"2fd7fa88-dd6f-4e59-acf8-e93e90d28502","plan_hash":"e2eeb5b23fef525779a9792efc218c5e18f36c45cdecc5b6e8a363d303b29f98","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"d092ccbb625fb8dd32fd9515eae61e82e6244def86979abd71e1a31d12aef9e0","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":11,"total":15},"evidence_id":"2fd7fa88-dd6f-4e59-acf8-e93e90d28502","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"9f0d6199155fac32250afb570b965292241a238b2eee43d41ef6026f6056dca9","status":"valid"},"source_digest":"bf930d267a1a56851224995f9898246cde1f838af15ac4486fddee99b0223296","version":1},"findings":[{"category":"gobby-format","check_key":"acceptance-target-parity","description":"Section 1.1 cannot complete acceptance 1.1.8 from its own scope: it defines model and schema work, while the production write/read path required for the two-carrier round trip is introduced in section 1.3.","finding_id":"R10-F01","fix":"Narrow 1.1.8 to structural model/schema support for both carriers. Keep production insert/read round-trip and one-side-preservation coverage solely in 1.3.10.","location":"P1 / §§1.1 and 1.3","prevention":"For every acceptance verb and named artifact, verify that the owning section targets the production symbols needed to satisfy it and that later-phase behavior stays in its later owner.","principle":"Each acceptance item must be executable within its owning leaf's declared targets and dependencies.","root_cause":"Acceptance 1.1.8 assigns production writer/reader round-trip behavior to section 1.1 even though those implementation targets and responsibilities are owned by section 1.3.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","check_key":"privilege-manifest-contract-inventory","description":"The exact relation-set and source-inventory contract in tests/code_index/test_gcode_privilege_manifest.py will become stale across two leaves, yet neither leaf declares the complete contract surface.","finding_id":"R10-F02","fix":"Add tests/code_index/test_gcode_privilege_manifest.py to sections 1.1 and 1.3. Update relation/sequence parity in 1.1, then retarget crates/gcode/security/managed_postgres_privileges.json in 1.3 so source_inventory reflects the new database call sites.","location":"P1 / §§1.1 and 1.3","prevention":"When a manifest test asserts exact managed objects or source call counts, map every relation, sequence, and PostgreSQL call-site change to both the manifest and its contract test.","principle":"Exact inventory contracts must be targeted in every section that changes the inventory they assert.","root_cause":"The plan targets the managed privilege manifest only in section 1.1, omits its exact-set Python contract test, and then adds PostgreSQL calls in section 1.3 that change the manifest's source inventory.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"inheritance-content-gc-graph-facts","description":"An inheritance-only content version can be judged to have no graph facts, skip FalkorDB deletion, and then lose its authoritative PostgreSQL row, stranding the projected inheritance edge.","finding_id":"R10-F03","fix":"Target the content-GC discovery predicate and its tests in section 2.2, include code_inheritance in has_graph_facts, and add an inheritance-only content-version regression.","location":"P2 / §2.2","prevention":"Whenever a new projected fact table is added, enumerate every graph-fact existence predicate and add a regression for content versions containing only that fact class.","principle":"Every projected fact class must participate in the content-GC decision that governs projection deletion.","root_cause":"The current has_graph_facts predicate checks code_symbols, code_imports, and code_calls only; the plan adds code_inheritance without extending that decision point.","section_id":"2.2","severity":"blocking"},{"category":"missing-requirement","check_key":"external-schema-inventory-parity","description":"External database provisioning would omit code_inheritance even though runtime validation and queries require it.","finding_id":"R10-F04","fix":"Add crates/gcore/src/schema/external.rs::gcode_postgres_objects and its schema inventory test to section 1.1, including the table, constraints, indexes, and required privileges.","location":"P1 / §1.1","prevention":"For each new runtime relation, enumerate embedded migrations, external provisioning inventories, validators, grants, indexes, and schema parity tests.","principle":"Every production schema-provisioning path must create the complete runtime schema.","root_cause":"gcore::schema::external::gcode_postgres_objects is a complete external-schema inventory that currently ends at code_calls, but section 1.1 does not target it for code_inheritance.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"indexer-cleanup-fact-inventory","description":"The stated exhaustive cleanup requirement is not traceable to two concrete indexer test seams, so stale-row deletion and shared-fact retention can regress without acceptance coverage.","finding_id":"R10-F05","fix":"Add crates/gcode/src/index/indexer/stale_cleanup_tests.rs and crates/gcode/src/index/indexer/tests/api_contract.rs to section 1.3, with a code_inheritance cleanup assertion and a shared-fact retention negative assertion.","location":"P1 / §1.3","prevention":"Search every authoritative-fact table inventory for delete, retain, and negative assertions, then declare each affected production and test symbol in the owning leaf.","principle":"An exhaustive cleanup and retention requirement must name every test seam that encodes the fact-table inventory.","root_cause":"Section 1.3 says every cleanup that deletes code_calls must also delete code_inheritance, but its Targets omit stale_cleanup_tests.rs and api_contract.rs, which encode cleanup and shared-fact retention behavior.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"local-import-active-content-resolution","description":"A retained old symbol can create false ambiguity or promote an edge to an inactive provider, and inactive pending LocalImport rows can be processed alongside the current version.","finding_id":"R10-F06","fix":"Join provider and pending-row resolution to active file state and content hash, apply overlay shadowing before uniqueness checks, and add old/current-provider plus inactive-pending-row regressions.","location":"P1 / §§1.2–1.3","prevention":"For every symbol-resolution path, verify project, file identity, active content hash, overlay visibility, and retained-history behavior before applying uniqueness or ambiguity rules.","principle":"Symbol promotion must resolve providers and owners only from active visible content versions.","root_cause":"LocalImport provider resolution queries code_symbols by project, path, and name without the active content hash, while project-wide pending-import reads include inactive retained rows.","section_id":"1.3","severity":"blocking"},{"category":"missing-requirement","check_key":"overlay-graph-composition","description":"Parent-defined classes and inheritance edges can disappear from overlay FCG, MCG, and CHG results, or be resolved with incorrect ownership, because the graph composition contract is unspecified.","finding_id":"R10-F07","fix":"Define the cross-project overlay graph contract and add targets/tests for parent providers, parent seeds, shadowing and tombstones, and FCG/MCG/CHG traversal over overlay plus unshadowed parent data.","location":"P1/P3 / §§1.3 and 3.1–3.4","prevention":"For every overlay-aware feature, trace parent providers, overlay shadows, tombstones, stored ownership, seed discovery, traversal, and output provenance across the project boundary.","principle":"Overlay graph behavior must compose overlay data with unshadowed parent data while preserving project provenance.","root_cause":"Existing visibility semantics are overlay plus unshadowed parent, while planned promotion, FalkorDB writes, and graph reads remain scoped to a single project and the plan does not define cross-project composition.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"visible-owner-content-identity","description":"A stale edge owned by an inactive content version can pass path-only visibility, consume a bound, and hide a valid active edge even when refill logic is present.","finding_id":"R10-F08","fix":"Carry owner project, owner path, and owner content hash—or an equivalent active identity—through internal graph rows, filter and refill before bounds, and add same-path old/current relationship regressions.","location":"P3 / §§3.1–3.4","prevention":"At every visibility-before-limit boundary, carry and compare project, path, and active content identity for each candidate row before quota counting and refill.","principle":"Visibility filtering before bounds must compare the full relationship-owner identity.","root_cause":"The planned visibility helper yields a path-only set, while graph rows discard owner project, owner path, and content hash, making current and stale relationships for the same path indistinguishable.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"chg-keyset-edge-id-cursor","description":"The caller cannot construct the specified unique next-page cursor because id(r) is absent from the returned row, leaving duplicate-key pagination ambiguous.","finding_id":"R10-F09","fix":"Return id(r) AS edge_id in the internal CHG row, use it in the cursor predicate and next-cursor construction, add a duplicate source/target/type pagination test, and omit edge_id only from the public payload.","location":"P3 / §3.4","prevention":"For each keyset query, compare the ORDER BY tuple, cursor predicate, returned cursor fields, and pagination tests component by component.","principle":"Every component of a total-order keyset cursor must be returned by the internal query.","root_cause":"The CHG plan orders by source, target, type(r), and id(r), then specifies an internal RETURN containing only source, target, and type.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"projection-sync-content-hash-cas","description":"A timed-out worker can finish late and falsely certify a newer, unprojected content version as graph-synced, suppressing recovery work and exposing stale graph state.","finding_id":"R10-F10","fix":"Capture the exact content hash for projection, make mark_graph_synced conditional on that hash still being current, treat a failed compare-and-set as stale completion, and add a timeout/reindex/late-completion regression.","location":"P1/P2 / §§1.3–2.2","prevention":"At every asynchronous projection completion, bind status mutation to the captured content hash with a compare-and-set against still-current file state.","principle":"Only the exact content version successfully projected may transition to graph_synced.","root_cause":"A detached projection worker captures old work, while mark_graph_synced resolves the currently active file state by path; after a timeout and reindex, the old worker can mark the new hash synced.","section_id":"1.3","severity":"blocking"},{"category":"weak-testability","check_key":"outgoing-emitted-edge-refill","description":"An outgoing prior-hop edge can consume the outgoing limit and suppress a valid edge without any planned test detecting the defect.","finding_id":"R10-F11","fix":"Add a shared outgoing exclude-and-refill regression in section 3.1 and depth-2 FCG and MCG outgoing cases at limit 1 in sections 3.2 and 3.3.","location":"P3 / §§3.1–3.3","prevention":"For each direction-specific traversal branch, test prior-hop exclusion, hidden-row filtering, limit consumption, refill, and depth greater than one.","principle":"Distinct directional quota branches require symmetric regressions for emitted-edge exclusion and refill.","root_cause":"Incoming and outgoing traversal branches count and refill separately, but every planned emitted-edge quota regression exercises only the incoming branch.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"decf6ca6-cdc6-49cd-88a8-26b84e675cf7","round":10,"round_number":10,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

Review attempt 11 coordinator ballot (unattended). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted slim: F01 project_id/project_root view envelope; F02 CHG both-endpoint project bind plus two-project collision test; F03 MCG asymmetric OR-aggregation test (sibling of 3.2.4); F05 3.3 depends on 3.2 so graph_query.rs is not a concurrent sibling; F06 failed mark_graph_synced CAS re-dirties the live row (no generation lock); F07 adopt_file_state paths join the promotion trigger, projection lists unchanged; F10 callees and nested graph view in code_navigation plus tool-chat, graph mutators stay mutating; F11 file-wide languages.rs, mutation.rs, sync_plan.rs, lifecycle.rs compile closure. Declined: F04 closed #17613 exploration measurement (not an #17680 bar, not preserved on #17678); F08 extra C#/Java declaration-index scan (heritage follows extract_calls tiers); F09 vector/Qdrant mark_vectors_synced CAS (pre-existing sibling sink; this plan does not change vector writers). validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"4573a331-bd9c-4188-b082-dea8ee049cdd","plan_hash":"1e73cb2e149e91c00583279fad645a84e0fa9c4be387a0f61c04bca0edf261d6","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"e37d1e49835f0fedb62ae11dee618fe5f6ceb7cb3f71ba567eb2c1b68082405d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":11,"total":13},"evidence_id":"4573a331-bd9c-4188-b082-dea8ee049cdd","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"b472c216b6868532e4e1dcc59431ce0a26c6f68fbf2ac5613e24d190a1d4f2a5","status":"valid"},"source_digest":"e62c744c5a4671c194490f7d8615772f58535fff0587b08075336326d06f5c1f","version":1},"findings":[{"category":"missing-requirement","check_key":"graph-view-project-context","description":"The planned graph-view payload and pinned graph_payload_keys omit project_id and project_root even though the governing CLI contract requires project identity and requires project_root when path context matters.","finding_id":"R11-F01","fix":"Add project_id and project_root to the shared view payload, renderer assertions, graph_payload_keys, generated and vendored contracts, gcode CLI documentation, and 3.1/3.5 acceptance tests proving they identify the resolved project.","location":"3.1","prevention":"For every new JSON view, trace the shared CLI envelope fields into implementation, generated and vendored contracts, documentation, and acceptance assertions.","principle":"Machine-readable navigation payloads consumed across project boundaries must identify the resolved project whenever their contents include project-relative paths or seeds.","root_cause":"The view schema was specified from graph traversal fields without carrying forward the governing CLI project-context envelope.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"chg-project-scoping","description":"The dedicated class-hierarchy reader never requires the governing project property in its FalkorDB MATCH predicates, so identical endpoint IDs from another project can contaminate ancestry or descendant results.","finding_id":"R11-F02","fix":"Require every CHG ancestor and descendant page query to bind the resolved project on both endpoints for CodeSymbol, ExternalSymbol, and UnresolvedCallee, and add a two-project collision regression.","location":"3.4","prevention":"For each new graph query, assert project predicates on all endpoint label partitions and test colliding identities in two projects.","principle":"Every read from the shared code graph must scope both endpoint identities to the resolved project.","root_cause":"The dedicated CHG traversal specifies labels and edge kinds but leaves the project predicate implicit.","section_id":"3.4","severity":"blocking"},{"category":"weak-testability","check_key":"mcg-asymmetric-truncation","description":"Section 3.3 requires incoming_truncated and outgoing_truncated to OR-aggregate independently, but no MCG acceptance case proves both remain true when raised at different executed frontiers.","finding_id":"R11-F03","fix":"Add a depth-2 IMPORTS fixture and focused MCG test where incoming truncates at one frontier and outgoing at another; assert both top-level flags remain true and output membership stays deterministic.","location":"3.3","prevention":"Pair every multi-frontier OR-aggregation rule with an asymmetric fixture that proves one frontier cannot clear an earlier flag.","principle":"Independent state flags aggregated across iterative frontiers need an acceptance case where each flag is raised at a different frontier.","root_cause":"MCG acceptance covers per-direction refill but not persistence of asymmetric truncation across multiple hops.","section_id":"3.3","severity":"blocking"},{"category":"missing-requirement","check_key":"dualview-measurement-ownership","description":"The cited #17613 adoption finding assigns a small real-issue exploration-step comparison to FCG acceptance, but this plan neither owns the measurement nor explicitly preserves it on #17678.","finding_id":"R11-F04","fix":"Add the fixed-issue before/after exploration measurement and durable evaluation artifact to 3.2 and the #17680 criteria update in 3.5, or explicitly retain and preserve the requirement on #17678.","location":"3.2","prevention":"When adopting a research finding, map every acceptance bar to a deliverable or name the task that retains it.","principle":"An adopted requirement must have an explicit implementation or acceptance owner, or an explicit preserved deferral owner.","root_cause":"The plan adopts FCG scope from #17613 but drops that finding's real-issue before/after measurement during task-criteria redistribution.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"graph-query-sibling-collision","description":"Sections 3.2 and 3.3 can run concurrently but both own crates/gcode/src/codewiki_facts/graph_query.rs::*, creating a predictable shared-file collision.","finding_id":"R11-F05","fix":"Make 3.3 depend on 3.2, or move all shared graph_query implementation into 3.1 and leave 3.2 and 3.3 with disjoint view-specific targets.","location":"3.3","prevention":"Before manifest handoff, compare sibling target sets and serialize or centralize every overlap.","principle":"Deliverables eligible for concurrent expansion must have disjoint implementation scopes or explicit dependency ordering.","root_cause":"Both sibling graph-view deliverables claim graph_query.rs::* while depending only on the same predecessor.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"late-graph-write-recovery","description":"A timed-out H1 worker can mutate FalkorDB after H2 is projected and marked synced. Rejecting H1's completion CAS neither reverses H1 mutations nor clears H2's synced marker, so recovery can remain suppressed over a stale graph.","finding_id":"R11-F06","fix":"On failed post-write content-hash CAS, atomically re-dirty the currently active file row and clear graph_sync_attempted_at, then add an H1-timeout/H2-success/late-H1 mutation regression proving H2 is reprojected.","location":"1.3","prevention":"For every detached timeout-prone writer, test the sequence old worker stalls, new version succeeds, old worker mutates, and old completion loses.","principle":"Rejecting stale completion is insufficient when the stale worker can already have performed irreversible external mutations; recovery must dirty the live version.","root_cause":"The proposed content-hash CAS guards only the completion marker, after FalkorDB writes have occurred.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"adopted-provider-promotion","description":"An already-persisted provider made active by adopt_file_state is not added to indexed_file_paths, so pending inheritance owners receive no candidate-appeared trigger and can remain stranded.","finding_id":"R11-F07","fix":"Define a promotion-trigger path set containing parsed files plus newly adopted active providers in ordinary and overlay pipelines, while keeping projection paths separate; add a shared-content adoption regression.","location":"1.3","prevention":"Inventory parse, adoption, restore, and overlay visibility transitions whenever designing provider-appearance triggers.","principle":"Every transition that makes a provider visible must trigger promotion of retryable consumers, regardless of whether the provider was parsed in the current run.","root_cause":"Candidate-appearance recovery is keyed only by indexed_file_paths and omits files activated through adopt_file_state, including overlay adoption.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"explicit-consumer-first-carrier","description":"For C# and analogous declaration-indexed languages, explicit consumer-only indexing can classify an unindexed in-repo provider as external and persist no LocalImport carrier for later promotion.","finding_id":"R11-F08","fix":"Define how explicit consumer-only indexing discovers or preserves not-yet-indexed local candidates, and add a C# or Java consumer-first/provider-later test that first persists LocalImport and later promotes the same row.","location":"1.2","prevention":"Test consumer-only explicit indexing for every resolution strategy that depends on declaration-derived project context.","principle":"Consumer-before-provider indexing must preserve a retryable local candidate carrier before terminal external classification.","root_cause":"The explicit-file resolution context contains indexed files and explicit ASTs only, so undiscovered in-repo provider declarations are absent from language indexes.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"vector-versioned-completion","description":"A late H1 vector worker can overwrite stable point IDs or leave H1-only points after H2 succeeds, then mark the current path's hash synced through the existing path-only completion update.","finding_id":"R11-F09","fix":"Capture the active content hash for vector projection, CAS mark_vectors_synced against it, re-dirty the live vector state after stale post-upsert completion, and add the matching late-old-worker regression.","location":"1.3","prevention":"After fixing an async stale-writer race, sweep every sibling sink on the shared worker boundary and require symmetric sequence tests.","principle":"Adjacent projection branches sharing a detached timeout boundary need the same versioned write and completion invariant.","root_cause":"The plan hardens graph completion but leaves vector fetch, Qdrant writes, and completion path-scoped within the same late-worker race.","section_id":"1.3","severity":"blocking"},{"category":"missing-requirement","check_key":"navigation-consumer-registries","description":"callees is absent from the navigation and read-only registries, while graph is classified only at top-level command granularity, so graph view cannot be authorized as read-only without also exposing destructive graph subcommands.","finding_id":"R11-F10","fix":"Update code_navigation.py, _tool_chat_tools.py, and their tests: register callees as read-only navigation and recognize exactly the nested graph view command as read-only while retaining graph clear/rebuild/sync as mutating.","location":"3.1","prevention":"For every CLI command addition, search all command registries, policy allowlists, normalization tables, schemas, and their tests.","principle":"A new CLI navigation surface is complete only when every policy and registry consumer can classify and authorize it at the correct mutability.","root_cause":"The plan updates CLI and skill contracts but omits the hook navigation registry and tool-chat allow/read-only policy.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"exact-target-caller-closure","description":"The exact targets omit the 21 LanguageSpec literals, SyncFileMutation constructors and tests, rebuild_project_graph call sites, mark_graph_synced callers, and the graph-fact emptiness helper test required by sections 1.2, 2.1, and 2.2.","finding_id":"R11-F11","fix":"Widen languages.rs, graph/code_graph/write/sync_plan.rs, and graph lifecycle targets to justified file-wide scope or enumerate every affected literal, constructor, caller, and test, with acceptance proving compile-time and behavioral closure.","location":"1.2","prevention":"Run a class-wide constructor/caller/destructure sweep for every changed type and signature, then enumerate all sites or justify file-wide scope.","principle":"When a plan changes a struct field or function signature, its target inventory must include every constructor, literal, caller, destructure, and focused test required to compile and preserve behavior.","root_cause":"Several targets are narrowed to defining symbols even though adjacent same-file constants, test literals, rebuild callers, and helper tests necessarily change.","section_id":"1.2","severity":"blocking"}],"reviewer_session":"419a08d7-439d-46e1-8833-310e1d48f60d","round":11,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

**Review attempt 13** `kind: verification`

- reviewer_run: fdaef92f-db8e-4299-8b18-d61d84a2f6ff
- reviewer_session: 7f1ca34a-b8d3-49c3-8fbb-6cf055ec5f20
- evidence_id: c3960c31-ed1a-4417-a823-913ad83ae11b
- verdict: protocol_failure (`shadow_manifest_mismatch`); no `coverage_attestation`; attempt does not count toward `completed_plan_review_rounds`
- findings_presented: R13-F01–R13-F09 (all blocking; salvage)
- accepted:
  - R13-F01 slim: heritage deletes bind `{project: $project}` on both generic endpoints
  - R13-F02 slim: MCG keeps typed provider-file and module-name frontier sets
  - R13-F03 slim: CodeSymbol→ExternalSymbol / UnresolvedCallee writer test
  - R13-F05 slim: 2.1 closes `sync_file_graph` and lifecycle callers with empty slices
  - R13-F06 slim: `CodewikiFacts::edges`, `commands/graph.rs` callees re-export, skill tests
  - R13-F08 slim: nullable `nodes[].file`
  - R13-F09 slim: seed resolution propagates PostgreSQL errors
- declined:
  - R13-F04 pin `392_code_inheritance.sql` — re-litigates R12 hop-head freshness; implementers take max(file+registry)+1 at start
  - R13-F07 owner-identity visibility type — re-litigates R6-F10 / R10-F08 / R12-F07
- resolution_notes: Unattended ballot against compile/correctness/security-parity/production-path. Official V1 adversary fence was not written; evidence expired.

Review attempt 14 coordinator ballot (unattended). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. Accepted slim R14-F01: one MCG provider equivalence class E(P) from existing ImportResolutionContext invert helpers plus the 2.2 active visible code_imports (source_file, target_module) reader; both seed forms walk {file:P} union that alias set; two-alias/one-provider fixture 3.3.15. Do not add a code_imports column, ownership table, or IMPORTS provider_file property. Declined R14-F02: per-file projection advisory fence re-litigates R3-F11 generation lock / extra mechanism; remaining zombie-writer-without-CAS race is pre-existing for CALLS and is not solved by this plan's accepted CAS plus failed-CAS dirty. validate_plan valid=true, symbol_validation passed. Do not write M1.

```json plan-review-round
{"evidence_id":"b948bae8-a570-4b95-aa44-687924f5f5e6","plan_hash":"d6b7080089e6783e455c43c61de1e894d9aa819ce41e56f21115b6c75d106fb5","round_number":14,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a6ce91bea2aaa1bf99f311696e4bd5190009f38997ebff48cadfa08b2bd2e94d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":2,"total":3},"evidence_id":"b948bae8-a570-4b95-aa44-687924f5f5e6","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"77670091bac1dea48723f88848bd3f2d053cff15cc80d8806194c19fc0725baf","status":"valid"},"source_digest":"33a1ae4f08f24e347b4e9faf3be9e31d3f471e2d353c2da2fc45e646c2a7a64b","version":1},"findings":[{"category":"unhandled-edge","check_key":"mcg-provider-file-reverse-equivalence","description":"Section 3.3 requires a provider-file seed and every uniquely resolving module-name seed to produce the same typed frontier, consumers, dependencies, and directional flags. The current facts and proposed helper expose only module-to-provider resolution. When two raw module specifiers resolve to one provider, the file seed cannot discover the complete module-key set without a project-wide import scan, so the stated equivalence is not implementable within the plan's bounded-query contract.","finding_id":"R14-F01","fix":"Extend the plan with one canonical provider equivalence-class operation used by both seed forms. Persist the uniquely resolved provider file on each local IMPORTS fact/edge (no separate ownership table), query all active visible module aliases by that provider before the bounded Incident walk, and add a two-alias/one-provider fixture proving all three seeds return identical nodes, edges, and flags.","location":"P3 / §3.3","prevention":"For every promised alias or seed equivalence, verify both mapping directions from persisted facts, including multiple raw and relative aliases, before declaring the traversal bounded and complete.","principle":"Equivalent seed forms must resolve through one complete bidirectional identity mapping before a bounded traversal begins.","root_cause":"The plan reuses one-way module/importer-to-provider candidate helpers, while persisted IMPORTS records only consumer file to raw module name; a provider file therefore has no bounded way to recover every active module alias that targets it.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"detached-projection-writer-fence","description":"The planned CAS fixes stale workers that reach mark_graph_synced, but not one that resumes after H2 has projected and marked the live row synced, performs a stale Falkor batch or stale-token deletion, then panics, hangs again, or terminates before its CAS. In that reachable branch no failed CAS re-dirties the row, so pending recovery is suppressed over stale or partial graph state.","finding_id":"R14-F02","fix":"Add a per-(project_id,file_path) projection fence, using the existing PostgreSQL advisory-lock mechanism or an equivalent single bounded guard, held across mark-attempt, every Falkor batch, stale deletion, and completion CAS. A later attempt that cannot acquire the fence must leave the active row pending and defer mutation. Add the timeout/H2/late-H1/exits-before-CAS regression.","location":"P1 / §1.3 and P2 / §2.2","prevention":"Test every detached projection with the sequence old worker times out, new attempt succeeds, old worker mutates late, and old worker exits before completion; require the live row to remain pending until one fully fenced attempt finishes.","principle":"A detached external writer must be fenced for the entire mutation, not only guarded at its completion marker.","root_cause":"The planned content-hash/attempt CAS runs only after all Falkor mutations; a timed-out stale worker that mutates after a newer success and exits before completion never executes the failed-CAS re-dirty path.","section_id":"1.3","severity":"blocking"}],"reviewer_session":"6a99d0f7-5933-4ac6-83d2-b13b125c5f4a","round":14,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

Review attempt 15 coordinator ballot (unattended, review cap reached). Verdict needs_review with valid coverage_attestation; this attempt counts toward completed_plan_review_rounds. This is the last capped adversary. Human requested convergence, M1 handoff, and expand. Accepted slim R15-F01: close each newly discovered MCG frontier through the existing E(X) operation (file adds E(file); uniquely resolved module adds provider file plus E(provider); missing/ambiguous providers stay terminal). Depth-two P→Q→R fixture 3.3.16. No persisted provider store. Human handoff follows this checkpoint: write M1 and expand; do not launch another adversary.

```json plan-review-round
{"evidence_id":"bd724e7e-0880-4778-acdc-3055e87c43e3","plan_hash":"b50b2c36be1550bfb2affa6edd23c8aa8eb3a97d14a11954857cc9c1637d189e","round_number":15,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6bd4cf73e65fda658af67ff77b3c5fda1308ab82bacea63b89a763fe5abfe68f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":1,"total":1},"evidence_id":"bd724e7e-0880-4778-acdc-3055e87c43e3","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"6df79adf47e0b17ebcd04cacf8e3122b65fdc307535caf246e570b7449596354","status":"valid"},"source_digest":"f231134c1c175e8ceb2fd0a92e9d93d1a4a00bb0e74887c13ca9b621ef366b80","version":1},"evidence_id":"bd724e7e-0880-4778-acdc-3055e87c43e3","findings":[{"category":"unhandled-edge","check_key":"mcg-frontier-equivalence-closure","description":"The initial file/module seed pair is equivalent, but the equivalence is lost after hop one. In a chain where file P imports an alias of Q and Q imports an alias of R, reaching the Q module node cannot add Q's provider file or E(Q), so depth-two MCG omits Q's outgoing dependency on R and yields different semantics depending on whether Q was seeded or discovered.","finding_id":"R15-F01","fix":"Amend §3.3 so each newly discovered frontier is closed through the existing provider equivalence operation before the next scoped hop: a uniquely resolved module adds its provider file and all E(provider) aliases, a newly discovered file adds E(file), and missing or ambiguous providers remain terminal. Add a depth-two P→Q→R fixture proving P's file seed and every alias seed reach the same second-hop nodes, edges, bounds, and direction flags without adding a persisted provider store.","location":"P3 / §3.3","prevention":"For every multi-hop alias graph, test a depth-two dependency chain and verify canonical identity closure is applied to every newly discovered frontier before edge bounds and truncation are classified.","principle":"Zero-cost provider/module identity equivalence used to define an MCG node must close every traversal frontier, not only the initial seed.","root_cause":"Section 3.3 computes E(P) only during seed canonicalization, while persisted IMPORTS is CodeFile-to-CodeModule with no provider bridge; a newly discovered module therefore cannot expose its provider file's outgoing dependencies on the next hop.","section_id":"3.3","severity":"blocking"}],"reviewer_session":"478a50c4-99c6-4eb9-aa47-90369e9630b3","round":15,"round_number":15,"verdict":"needs_review"},"session_id":"2e629c7b-9e54-4b36-812b-6271b565d7b5"}
```

## T1 Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| P1 Inheritance facts | #20512 | open epic |
| 1.1 Add inheritance model, schema, and privileges | #20515 | ready |
| 1.2 Extract explicit heritage clauses | #20516 | blocked by #20515 |
| 1.3 Persist inheritance facts on index | #20517 | blocked by #20516 |
| P2 FalkorDB projection | #20513 | open epic |
| 2.1 Project inheritance edges | #20518 | blocked by P1 |
| 2.2 Delete and rebuild inheritance edges | #20519 | blocked by #20518 |
| P3 View surface | #20514 | open epic |
| 3.1 Add shared graph view payload and Mermaid | #20520 | blocked by P2 |
| 3.2 Add FCG view and callees command | #20521 | blocked by #20520 |
| 3.3 Add MCG view with Leiden communities | #20522 | blocked by #20520, #20521 |
| 3.4 Add class-hierarchy view | #20523 | blocked by #20520, #20519 |
| 3.5 Update CLI contract and code-index skill | #20524 | blocked by #20521–#20523 |

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add inheritance model, schema, and privileges
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `InheritanceRelation` and `HeritageKind` exist and
    `ParseResult` carries `inheritance`. file: `crates/gcode/src/models.rs`.

    1.1.2: `code_inheritance` is in baseline, the next unused hop (`max(existing embedded
    versions and migration filenames)+1`, never a version that already exists), privileges,
    and `REQUIRED_TABLES`. file: `crates/gcore/assets/schema/baseline.sql`.

    1.1.3: Runtime schema validation requires the new table and columns. symbol: `validate_runtime_schema`.

    1.1.4: `code_inheritance` is in the generated gcode project-policy inventory next
    to `code_calls`; same-project managed-role access succeeds and cross-project access
    is rejected. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_has_gcode_project_policies`.

    1.1.5: Serial DB and parser `ParseResult` literals compile with the new `inheritance`
    field. file: `crates/gcode/src/index/indexer/tests/serial_db.rs`.

    1.1.6: Catalog identity consumers advance with the hop: `schema_expected_identity.json`,
    `schema_contract.rs`, grant golden `latest_version`, and `gdaemon` cli contract.
    file: `src/gobby/storage/schema_expected_identity.json`.

    1.1.7: `code_inheritance` has `code_inheritance_pkey`, `code_inheritance_unique_target`
    (`UNIQUE NULLS NOT DISTINCT` including `source_external_module` and `target_external_module`),
    and `code_inheritance_content_fkey` `ON DELETE CASCADE` to `code_indexed_files`.
    file: `crates/gcore/assets/schema/baseline.sql`.

    1.1.8: `InheritanceRelation` and `code_inheritance` structurally support independent
    `source_external_module` and `target_external_module` carriers (model fields,
    helpers, and schema columns). This leaf does not implement production insert/read
    or one-side-only promotion; those stay in 1.3.10. file: `crates/gcode/src/models.rs`.

    1.1.9: The `CallTargetKind::LocalImport` comment documents call-miss (`Unresolved`,
    carrier dropped) versus inheritance-miss (row stays `LocalImport` with the side''s
    carrier). symbol: `CallTargetKind`.

    1.1.10: `code_inheritance_heritage_kind_check` exists and rejects every value
    outside `INHERITS`, `EXTENDS`, and `IMPLEMENTS`. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_heritage_kind_check_rejects_unknown`.

    1.1.11: After regenerating the four runtime-grant goldens, `tests/runtime_grants/test_golden_vectors.py`
    validates each file''s canonical bytes, `payload_checksum`, and signature against
    the new schema identity. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.

    1.1.12: `gcode_postgres_objects` creates `code_inheritance` with the unique constraint,
    content-version FK, and the three `code_calls`-shaped indexes. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_is_in_gcode_postgres_objects`.

    1.1.13: The managed privilege exact relation set includes `code_inheritance` next
    to `code_calls`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.

    1.1.14: Standalone adoption classifies a pre-inheritance eight-table code-index
    schema and applies `code_inheritance` without replaying `CREATE TABLE` against
    an already-provisioned external relation. test: `crates/gcore/src/schema/runner_tests.rs::code_inheritance_adoption_preserves_pre_inheritance_and_skips_existing`.'
  labels:
  - covers:class-hierarchy-graph:1.1:1.1.1
  - covers:class-hierarchy-graph:1.1:1.1.2
  - covers:class-hierarchy-graph:1.1:1.1.3
  - covers:class-hierarchy-graph:1.1:1.1.4
  - covers:class-hierarchy-graph:1.1:1.1.5
  - covers:class-hierarchy-graph:1.1:1.1.6
  - covers:class-hierarchy-graph:1.1:1.1.7
  - covers:class-hierarchy-graph:1.1:1.1.8
  - covers:class-hierarchy-graph:1.1:1.1.9
  - covers:class-hierarchy-graph:1.1:1.1.10
  - covers:class-hierarchy-graph:1.1:1.1.11
  - covers:class-hierarchy-graph:1.1:1.1.12
  - covers:class-hierarchy-graph:1.1:1.1.13
  - covers:class-hierarchy-graph:1.1:1.1.14
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Extract explicit heritage clauses
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `LanguageSpec` has `inheritance_query` and query bodies
    live in `heritage.rs`. symbol: `LanguageSpec`.

    1.2.2: `parse_file_with_semantic` fills `ParseResult.inheritance`. symbol: `parse_file_with_semantic`.

    1.2.3: Same-file Python `class Derived(Base)` resolves `INHERITS` to `Base`. test:
    `crates/gcode/src/index/parser/tests/heritage.rs::python_subclass_resolves_same_file_base`.

    1.2.4: TypeScript `class D extends B implements I` emits `EXTENDS` and `IMPLEMENTS`.
    test: `crates/gcode/src/index/parser/tests/heritage.rs::typescript_extends_and_implements`.

    1.2.5: Rust `impl Trait for Type` and `trait Foo: Bar` emit `IMPLEMENTS` / `EXTENDS`.
    test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_impl_and_supertrait`.

    1.2.6: Go embedding emits `EXTENDS`; a type that only happens to satisfy an interface
    does not. test: `crates/gcode/src/index/parser/tests/heritage.rs::go_embedding_only`.

    1.2.7: Every other emitting language has a same-file base/derived fixture in that
    test module. test: `crates/gcode/src/index/parser/tests/heritage.rs::all_emitting_languages_have_same_file_pair`.

    1.2.8: `parser/tests.rs` declares `mod heritage` so the new module compiles and
    runs. file: `crates/gcode/src/index/parser/tests.rs`.

    1.2.9: Cross-file Rust `impl Trait for Type` emits `IMPLEMENTS` with a retryable
    LocalImport source; same-file Type remains a resolved Symbol source. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_impl_source_resolves_across_files`.

    1.2.10: `impl LocalTrait for external_crate::ExternalType` persists `source_kind
    = External`; an unresolved Type persists `source_kind = Unresolved`; neither is
    a LocalImport promotion candidate. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_impl_external_and_unresolved_sources`.

    1.2.11: C# `interface` bases are `EXTENDS`, `struct` bases are `IMPLEMENTS`, and
    a class `base_list` with imported or unresolved mixed bases stays `INHERITS`;
    a same-file class vs interface target is typed. test: `crates/gcode/src/index/parser/tests/heritage.rs::csharp_base_list_uses_inherits_when_kind_unproven`.

    1.2.12: Ruby `include`, `extend`, and `prepend` each emit `IMPLEMENTS` (not `INHERITS`).
    test: `crates/gcode/src/index/parser/tests/heritage.rs::ruby_mixins_emit_implements`.

    1.2.13: Inherent Rust `impl Type` (no Trait) emits no heritage row. test: `crates/gcode/src/index/parser/tests/heritage.rs::rust_inherent_impl_emits_nothing`.

    1.2.14: C, Elixir, Lua, Bash, YAML, and JSON parse without emitting heritage rows.
    test: `crates/gcode/src/index/parser/tests/heritage.rs::empty_query_languages_emit_no_rows`.

    1.2.15: An imported-stdlib or crate-qualified base persists `target_kind = External`
    with `target_external_module`; an unbound name persists `target_kind = Unresolved`
    with `target_name` and an empty target carrier. Neither is a LocalImport promotion
    candidate. test: `crates/gcode/src/index/parser/tests/heritage.rs::heritage_external_and_unresolved_targets`.'
  labels:
  - covers:class-hierarchy-graph:1.2:1.2.1
  - covers:class-hierarchy-graph:1.2:1.2.2
  - covers:class-hierarchy-graph:1.2:1.2.3
  - covers:class-hierarchy-graph:1.2:1.2.4
  - covers:class-hierarchy-graph:1.2:1.2.5
  - covers:class-hierarchy-graph:1.2:1.2.6
  - covers:class-hierarchy-graph:1.2:1.2.7
  - covers:class-hierarchy-graph:1.2:1.2.8
  - covers:class-hierarchy-graph:1.2:1.2.9
  - covers:class-hierarchy-graph:1.2:1.2.10
  - covers:class-hierarchy-graph:1.2:1.2.11
  - covers:class-hierarchy-graph:1.2:1.2.12
  - covers:class-hierarchy-graph:1.2:1.2.13
  - covers:class-hierarchy-graph:1.2:1.2.14
  - covers:class-hierarchy-graph:1.2:1.2.15
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Persist inheritance facts on index
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Indexing a file writes `code_inheritance` rows and
    deletes them with the content-version cleanup. symbol: `delete_content_version_non_symbol_facts`.

    1.3.2: `CodeFactSink` persists inheritance through the indexer file path. symbol:
    `CodeFactSink`.

    1.3.3: API insert reports inserted inheritance rows, not input length. test: `crates/gcode/src/index/api_tests.rs::api_upsert_inheritance_reports_rows_inserted_not_input_len`.

    1.3.4: Indexing a derived type that imports its base, then indexing the base file,
    promotes the same `LocalImport` row to `target_kind` Symbol with the canonical
    `target_symbol_id` and no duplicate. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_imported_base_promotes_after_base_indexes`.

    1.3.5: A miss leaves the row `LocalImport` with candidate files intact; a later
    base index promotes it. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_local_import_miss_stays_retryable`.

    1.3.6: Reindexing the derived file after the base is deleted or renamed rewrites
    the inheritance row the same way calls refresh; no extra provenance store. test:
    `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_invalidates_on_derived_reindex`.

    1.3.7: Pending inheritance readers live in `crates/gcode/src/db/queries.rs` and
    select rows by derived file and by project-wide LocalImport recovery, including
    a later-indexed Rust impl source Type. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_source_promotes_when_type_file_indexes`.

    1.3.8: When the imported base is already indexed, writing the derived file resolves
    `target_kind` to Symbol with the canonical id and exactly one row. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_imported_base_resolves_when_base_already_indexed`.

    1.3.9: When the Rust Type file is already indexed, writing `impl Trait for Type`
    resolves `source_kind` to Symbol with the canonical id and exactly one row. test:
    `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_impl_source_resolves_when_type_already_indexed`.

    1.3.10: A row that is LocalImport on both ends keeps distinct source and target
    candidate sets through insert and miss; promoting one side leaves the other carrier
    intact. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_keeps_independent_source_and_target_carriers`.

    1.3.11: Promoting a row owned by a file that was not in this run''s indexed set
    unions that owning path into graph projection and not into vector projection.
    symbol: `attach_projection_sync`.

    1.3.12: `gcode index` with projections enabled, after a provider-later promotion,
    graph-syncs the owning file and does not vector-sync that owner-only path. test:
    `crates/gcode/src/commands/index.rs::index_promotion_projects_owner_on_graph_only`.

    1.3.13: After a graph-only owner promotion, `IndexOutcome.projection_sync` lists
    the owner in `graph_file_paths` only, while the newly indexed provider is in both
    lists. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_pending_status_keeps_graph_only_owner`.

    1.3.14: Provider-later promotion with `sync_projections=false` sets the owner''s
    `graph_synced` to false and `graph_sync_attempted_at` to null and leaves `vectors_synced`
    unchanged, so a later pending-recovery scan can project the new typed edge. test:
    `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_promotion_dirties_owner_graph_pending`.

    1.3.15: After the new inheritance write/read call sites land, `managed_postgres_privileges.json`
    `source_inventory` matches the production PostgreSQL call inventory. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_covers_every_rust_database_call_at_head`.

    1.3.16: Project cleanup that deletes `code_calls` also deletes `code_inheritance`.
    test: `crates/gcode/src/index/indexer/stale_cleanup_tests.rs::cleanup_project_deletes_code_inheritance`.

    1.3.17: Invalidate retains `code_inheritance` as a shared fact next to `code_calls`.
    test: `crates/gcode/src/index/indexer/tests/api_contract.rs::invalidate_postgres_deletes_only_machine_state`.

    1.3.18: An old retained provider symbol and an inactive pending LocalImport row
    do not create false ambiguity or promote against inactive content; only the active
    content hash participates. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_promotion_uses_active_content_only`.

    1.3.19: A late `mark_graph_synced` after the path''s active hash changed does
    not mark the new hash synced. test: `crates/gcode/src/db/queries.rs::mark_graph_synced_cas_rejects_stale_hash`.

    1.3.20: After H2 projects hash B and CAS-succeeds, a late H1 Falkor write then
    failed CAS re-dirties the live B row (`graph_synced = false`, `graph_sync_attempted_at`
    null) so recovery reprojects B. test: `crates/gcode/src/db/queries.rs::mark_graph_synced_failed_cas_dirties_live_row`.

    1.3.21: Adopting an already-persisted shared-content provider (not parsed this
    run) promotes a pending LocalImport consumer; the adopted path is not added to
    `vector_file_paths`. test: `crates/gcode/src/index/indexer/tests/facts.rs::inheritance_adoption_promotes_pending_consumer`.

    1.3.22: Same-hash promotion: H1 reads pre-promotion facts, H2 promotes and projects
    the same owner hash, late H1 Falkor write then failed `(content_hash, graph_sync_attempted_at)`
    CAS re-dirties the live row so recovery keeps H2''s typed edge. test: `crates/gcode/src/db/queries.rs::mark_graph_synced_cas_rejects_same_hash_stale_attempt`.'
  labels:
  - covers:class-hierarchy-graph:1.3:1.3.1
  - covers:class-hierarchy-graph:1.3:1.3.2
  - covers:class-hierarchy-graph:1.3:1.3.3
  - covers:class-hierarchy-graph:1.3:1.3.4
  - covers:class-hierarchy-graph:1.3:1.3.5
  - covers:class-hierarchy-graph:1.3:1.3.6
  - covers:class-hierarchy-graph:1.3:1.3.7
  - covers:class-hierarchy-graph:1.3:1.3.8
  - covers:class-hierarchy-graph:1.3:1.3.9
  - covers:class-hierarchy-graph:1.3:1.3.10
  - covers:class-hierarchy-graph:1.3:1.3.11
  - covers:class-hierarchy-graph:1.3:1.3.12
  - covers:class-hierarchy-graph:1.3:1.3.13
  - covers:class-hierarchy-graph:1.3:1.3.14
  - covers:class-hierarchy-graph:1.3:1.3.15
  - covers:class-hierarchy-graph:1.3:1.3.16
  - covers:class-hierarchy-graph:1.3:1.3.17
  - covers:class-hierarchy-graph:1.3:1.3.18
  - covers:class-hierarchy-graph:1.3:1.3.19
  - covers:class-hierarchy-graph:1.3:1.3.20
  - covers:class-hierarchy-graph:1.3:1.3.21
  - covers:class-hierarchy-graph:1.3:1.3.22
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Project inheritance edges
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: "2.1.1: File sync MERGEs inheritance relationships tagged with\
    \ `content_hash` and `sync_token`. symbol: `plan_sync_batches`.\n2.1.2: Report\
    \ edge patterns include `INHERITS|EXTENDS|IMPLEMENTS`. file: `crates/gcode/src/graph/report/queries.rs`.\n\
    2.1.3: Writer contract includes the new Cypher. test: `crates/gcode/tests/contract.rs::code_graph_writer_matches_shared_schema_contract`.\n\
    2.1.4: Case-insensitive `INHERITS` / `inherits` map to 2.5 in `weight_for_kind`\
    \ alongside `EXTENDS` and `IMPLEMENTS`; unknown kinds stay 1.0. test: `crates/gcore/src/graph_analytics.rs::weight_for_kind_covers_observed_aliases_case_insensitively`.\n\
    2.1.5: `docs/contracts/shared-graph-schema.md` names `INHERITS`, `EXTENDS`, and\
    \ `IMPLEMENTS` with `CodeSymbol|ExternalSymbol|UnresolvedCallee` on either endpoint\
    \ and the provenance fields above; the existing writer contract still passes.\
    \ file: `docs/contracts/shared-graph-schema.md`.\n2.1.6: Owner-before-target-provider\
    \ and owner-before-Rust-source-provider file syncs MERGE endpoints and recover\
    \ the typed edge without a graph rebuild. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_recovers_when_owner_syncs_before_provider`.\n\
    2.1.7: Two same-endpoint, same-type inheritance facts on different lines survive\
    \ repeated sync as two relationships; CHG still public-dedups by `(source, target,\
    \ rel)`. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_keeps_parallel_same_type_facts`.\n\
    2.1.8: An `External` or `Unresolved` Rust impl source MERGEs `ExternalSymbol`\
    \ / `UnresolvedCallee` as the **source** endpoint and the typed `IMPLEMENTS` edge\
    \ survives file sync. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_external_and_unresolved_sources`.\n\
    2.1.9: A `CodeSymbol` source with an `External` or `Unresolved` base MERGEs `CodeSymbol\
    \ \u2192 ExternalSymbol` and `CodeSymbol \u2192 UnresolvedCallee` through `plan_sync_batches`,\
    \ with the typed relationship, owner file/line, `content_hash`, and `sync_token`.\
    \ test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_merge_external_and_unresolved_targets`.\n\
    2.1.10: `write.rs::sync_file_graph`, `lifecycle.rs` graph callers, and `projection/sync.rs::sync_graph_file`\
    \ compile against the new `sync_file` arity by passing `&[]` until 2.2. symbol:\
    \ `sync_file_graph`."
  labels:
  - covers:class-hierarchy-graph:2.1:2.1.1
  - covers:class-hierarchy-graph:2.1:2.1.2
  - covers:class-hierarchy-graph:2.1:2.1.3
  - covers:class-hierarchy-graph:2.1:2.1.4
  - covers:class-hierarchy-graph:2.1:2.1.5
  - covers:class-hierarchy-graph:2.1:2.1.6
  - covers:class-hierarchy-graph:2.1:2.1.7
  - covers:class-hierarchy-graph:2.1:2.1.8
  - covers:class-hierarchy-graph:2.1:2.1.9
  - covers:class-hierarchy-graph:2.1:2.1.10
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Delete and rebuild inheritance edges
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: File and content-hash deletes remove inheritance relationships.
    symbol: `delete_file_graph_queries`.

    2.2.2: Rebuild plans inheritance edges from PostgreSQL facts loaded by `read_graph_file_facts`.
    symbol: `read_graph_file_facts`.

    2.2.3: Combined delete Cypher includes inheritance rels. test: `crates/gcode/src/graph/code_graph/tests.rs::delete_queries_include_inheritance_rels`.

    2.2.4: Rebuild of a promoted imported-base inheritance row plans the expected
    typed heritage relationship. test: `crates/gcode/src/graph/code_graph/tests.rs::rebuild_projects_promoted_inheritance_edge`.

    2.2.5: An inheritance-only file is not treated as `no_graph_facts` and is synced.
    symbol: `has_no_graph_facts`.

    2.2.6: Derived-file reindex or rebuild after heritage rows change deletes previous
    inheritance edges whose `sync_token` no longer matches, including a missing token.
    test: `crates/gcode/src/graph/code_graph/tests.rs::rebuild_drops_stale_inheritance_after_derived_reindex`.

    2.2.7: `projection/sync.rs::sync_graph_file` projects inheritance facts; an inheritance-only
    file syncs outside the graph lifecycle CLI. symbol: `sync_graph_file`.

    2.2.8: Orphan cleanup retains `CodeSymbol`, `ExternalSymbol`, and unresolved nodes
    that remain live only through `INHERITS|EXTENDS|IMPLEMENTS`. test: `crates/gcode/src/graph/code_graph/tests.rs::cleanup_keeps_heritage_only_terminals`.

    2.2.9: Deleting or rebuilding the impl file removes the `IMPLEMENTS` edge even
    when the source Type is declared in another file. test: `crates/gcode/src/graph/code_graph/tests.rs::delete_cross_file_rust_impl_uses_relationship_owner`.

    2.2.10: Indexing the provider later, with projection enabled, projects the promoted
    typed heritage edge without a graph rebuild or owner-file reindex. test: `crates/gcode/src/graph/code_graph/tests.rs::promotion_projects_owning_file_without_rebuild`.

    2.2.11: Deleting the impl file removes an `IMPLEMENTS` edge whose source is `ExternalSymbol`
    or `UnresolvedCallee`; orphan cleanup still keeps a heritage-only source terminal
    that remains referenced. test: `crates/gcode/src/graph/code_graph/tests.rs::delete_external_source_impl_uses_relationship_owner`.

    2.2.12: An inheritance-only content version is `has_graph_facts=true` and participates
    in projection deletion before PostgreSQL GC. test: `crates/gcode/src/commands/status/content_gc/tests.rs::inheritance_only_content_has_graph_facts`.

    2.2.13: Whole-file, stale-token, and content-version heritage deletes bind `{project:
    $project}` on both generic endpoints and do not delete a colliding same-path edge
    in another project. test: `crates/gcode/src/graph/code_graph/tests.rs::heritage_delete_binds_project_on_both_endpoints`.'
  labels:
  - covers:class-hierarchy-graph:2.2:2.2.1
  - covers:class-hierarchy-graph:2.2:2.2.2
  - covers:class-hierarchy-graph:2.2:2.2.3
  - covers:class-hierarchy-graph:2.2:2.2.4
  - covers:class-hierarchy-graph:2.2:2.2.5
  - covers:class-hierarchy-graph:2.2:2.2.6
  - covers:class-hierarchy-graph:2.2:2.2.7
  - covers:class-hierarchy-graph:2.2:2.2.8
  - covers:class-hierarchy-graph:2.2:2.2.9
  - covers:class-hierarchy-graph:2.2:2.2.10
  - covers:class-hierarchy-graph:2.2:2.2.11
  - covers:class-hierarchy-graph:2.2:2.2.12
  - covers:class-hierarchy-graph:2.2:2.2.13
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Add shared graph view payload and Mermaid
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  validation_criteria: '3.1.1: `gcode graph view` exists on `GraphCommand` and is
    dispatched. symbol: `GraphCommand`.

    3.1.2: Every successful view payload includes a complete Mermaid block that passes
    `is_valid_mermaid`. symbol: `is_valid_mermaid`.

    3.1.3: Invalid `--view` is a clap / user error. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_rejects_unknown_kind`.

    3.1.4: One shared renderer matrix for fcg, mcg, and class-hierarchy: shuffled
    input is byte-identical after sort; quote, bracket, newline, UUID-like, and non-ASCII
    labels escape; colliding sanitized keys stay distinct via opaque `n0`/`n1` IDs;
    file and module nodes are included; the mermaid field is a complete fenced block
    accepted by `is_valid_mermaid`. test: `crates/gcode/src/commands/graph/view/render.rs::view_render_is_deterministic_and_escapes_hostile_labels`.

    3.1.5: `--depth` is optional `1..=16`; 0 and 17 are clap errors; 16 is accepted.
    Omitted `--depth` is effective 8 for `class-hierarchy` and 1 for `fcg` and `mcg`.
    test: `crates/gcode/src/cli/tests/projection.rs::graph_view_depth_domain`.

    3.1.6: `--incoming-limit` or `--outgoing-limit` with `--view=class-hierarchy`
    is rejected; both are accepted for `fcg` and `mcg`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_rejects_hierarchy_row_limits`.

    3.1.7: `commands/graph.rs` declares `mod view` and dispatch reaches the view command.
    file: `crates/gcode/src/commands/graph.rs`.

    3.1.8: Typed canonical ids keep a file path that equals a module name as two nodes
    through JSON, Mermaid tokens, and `AnalyticsGraph` dedup. test: `crates/gcode/src/commands/graph/view/mod.rs::view_typed_ids_keep_file_and_module_collision_distinct`.

    3.1.9: The shared visibility-before-bound helper, given synthetic edges, drops
    stale-hash, other-machine, and overlay-shadowed endpoints; terminals inherit visibility
    from their owning visible relationship. Live FCG, MCG, and CHG handlers may still
    be stubs. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_respects_active_visible_file_map`.

    3.1.10: Invisible synthetic rows ordered before enough visible rows do not consume
    an edge slot or hop-cut; parent-only and same-path overlay-shadowed edges are
    classified before `LIMIT` / page exhaustion. Live view handlers may still be stubs.
    test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_invisible_rows_do_not_consume_edge_or_hop_budget`.

    3.1.11: `crates/gcode/src/commands/graph/view/mod.rs`, `crates/gcode/src/commands/graph/view/render.rs`,
    `crates/gcode/src/commands/graph/view/fcg.rs`, `crates/gcode/src/commands/graph/view/mcg.rs`,
    `crates/gcode/src/commands/graph/view/class_hierarchy.rs`, and `crates/gcode/src/commands/graph/reads.rs`
    each stay under 1,000 lines. file: `crates/gcode/src/commands/graph/view/mod.rs`.

    3.1.12: `resolve_symbol` is `pub(super)` (or a sibling shared resolver) so 3.2
    and 3.4 consume it without editing an undeclared file. symbol: `resolve_symbol`.

    3.1.13: An Incident incoming frontier larger than `SCOPE_CHUNK_LEN` with more
    than `incoming_limit+1` cross-chunk internal edges ordered before genuine external
    incoming edges still returns those external edges, does not count the internals
    toward the incoming limit, and does not set `incoming_truncated` unless distinct
    eligible overflow remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::incident_incoming_excludes_cross_chunk_frontier_sources`.

    3.1.14: More than `limit+1` parallel storage relationships for one public `(source,
    target, rel)` pair ordered before other distinct public pairs yield every distinct
    eligible neighbor and set a direction flag only when distinct eligible overflow
    remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::scoped_limits_count_distinct_public_edges`.

    3.1.15: A shared formatter payload whose JSON plus Mermaid exceeds 10,000 characters
    parses as complete JSON, retains every expected node and edge for fcg, mcg, and
    class-hierarchy, includes the complete Mermaid fence, and passes `is_valid_mermaid`.
    test: `crates/gcode/src/commands/graph/view/render.rs::view_render_does_not_clip_above_historical_budget`.

    3.1.16: A scoped incoming fetch whose first-ordered public pair is in the exclude
    set still returns a later new eligible neighbor, does not count the excluded pair
    toward the limit, and does not set `incoming_truncated` unless new eligible overflow
    remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::scoped_limits_exclude_already_emitted_edges`.

    3.1.17: A scoped outgoing fetch whose first-ordered public pair is in the exclude
    set still returns a later new eligible neighbor, does not count the excluded pair
    toward the limit, and does not set `outgoing_truncated` unless new eligible overflow
    remains. test: `crates/gcode/src/codewiki_facts/graph_query.rs::scoped_limits_exclude_already_emitted_outgoing_edges`.

    3.1.18: Every successful view payload includes `project_id` and `project_root`
    identifying the resolved project. test: `crates/gcode/src/commands/graph/view/render.rs::view_payload_includes_project_identity`.

    3.1.19: Unconfigured and unreachable FalkorDB views set a non-null `hint` and
    empty nodes/edges; a configured empty graph has `hint: null`. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_unavailable_differs_from_empty`.

    3.1.20: `nodes[].file` is populated for CodeFile/CodeSymbol, uses the unique provider
    path for a uniquely resolved module, and is null for ExternalSymbol, UnresolvedCallee,
    and ambiguous or unowned modules. test: `crates/gcode/src/commands/graph/view/render.rs::view_node_file_nullability_by_kind`.

    3.1.21: A PostgreSQL connect or query failure during seed resolution is an infrastructure
    error; a successful empty or ambiguous read is the missing-seed user error, including
    the allow-stale dispatch path. test: `crates/gcode/src/commands/graph/view/mod.rs::graph_view_seed_resolution_propagates_database_errors`.'
  labels:
  - covers:class-hierarchy-graph:3.1:3.1.1
  - covers:class-hierarchy-graph:3.1:3.1.2
  - covers:class-hierarchy-graph:3.1:3.1.3
  - covers:class-hierarchy-graph:3.1:3.1.4
  - covers:class-hierarchy-graph:3.1:3.1.5
  - covers:class-hierarchy-graph:3.1:3.1.6
  - covers:class-hierarchy-graph:3.1:3.1.7
  - covers:class-hierarchy-graph:3.1:3.1.8
  - covers:class-hierarchy-graph:3.1:3.1.9
  - covers:class-hierarchy-graph:3.1:3.1.10
  - covers:class-hierarchy-graph:3.1:3.1.11
  - covers:class-hierarchy-graph:3.1:3.1.12
  - covers:class-hierarchy-graph:3.1:3.1.13
  - covers:class-hierarchy-graph:3.1:3.1.14
  - covers:class-hierarchy-graph:3.1:3.1.15
  - covers:class-hierarchy-graph:3.1:3.1.16
  - covers:class-hierarchy-graph:3.1:3.1.17
  - covers:class-hierarchy-graph:3.1:3.1.18
  - covers:class-hierarchy-graph:3.1:3.1.19
  - covers:class-hierarchy-graph:3.1:3.1.20
  - covers:class-hierarchy-graph:3.1:3.1.21
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Add FCG view and callees command
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: `gcode callees` returns outgoing CALLS through production
    `count_callees` / `find_callees` (not `find_callees_batch`). file: `crates/gcode/src/graph/code_graph/read/relationships.rs`.

    3.2.2: `--view=fcg` returns callers and callees with independent limits and truncation
    flags. symbol: `CodewikiFacts::scoped_edges`.

    3.2.3: Callees CLI matches callers pagination (`limit`/`offset`/`total`) and does
    not grow an output-clip token-budget flag. test: `crates/gcode/src/commands/graph/tests.rs::callees_mirrors_callers_pagination`.

    3.2.4: An asymmetric depth-2 fixture where only incoming truncates at one frontier
    and only outgoing truncates at another sets the two top-level flags independently
    and keeps the limited membership stable. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_or_aggregates_asymmetric_truncation`.

    3.2.5: More than 100 tied callees are returned in stable order across two pages.
    test: `crates/gcode/src/commands/graph/tests.rs::callees_paginates_past_max_graph_limit`.

    3.2.6: `Command::Callees` selects FalkorDB-only config next to `Command::Callers`.
    test: `crates/gcode/src/dispatch/tests.rs::graph_and_vector_commands_request_only_needed_services`.

    3.2.7: Local, external, and unresolved callee targets survive visibility; external/unresolved
    UUID ids are not required to exist in `code_symbols`. test: `crates/gcode/src/commands/graph/tests.rs::callees_keeps_external_and_unresolved_targets`.

    3.2.8: `--view=fcg` includes CodeSymbol, ExternalSymbol, and UnresolvedCallee
    CALLS targets within the declared bounds. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_includes_external_and_unresolved_targets`.

    3.2.9: Omitted `--depth` on `--view=fcg` walks one hop and serializes `depth:
    1`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_omitted_depth_defaults_by_kind`.

    3.2.10: `gcode callees` parses as a top-level command with the same `limit`/`offset`
    shape as callers. test: `crates/gcode/src/cli/tests/top_level.rs::test_parse_callees_remains_top_level`.

    3.2.11: `--view=fcg` applies the 3.1 visibility-before-bound helper before every
    `LIMIT` and hop expansion. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_applies_visible_owner_set_before_limits`.

    3.2.12: A cyclic CALLS fixture at `--depth > 2` expands each typed node once,
    emits each public edge once, and stays deterministic. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_cycle_emits_unique_nodes_and_edges`.

    3.2.13: A depth-2 incoming walk whose prior-hop public edge sorts before a new
    neighbor still includes that neighbor at `incoming-limit=1` and does not set `incoming_truncated`
    unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_prior_edge_does_not_consume_next_hop_quota`.

    3.2.14: A depth-2 outgoing walk whose prior-hop public edge sorts before a new
    neighbor still includes that neighbor at `outgoing-limit=1` and does not set `outgoing_truncated`
    unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/fcg.rs::fcg_prior_outgoing_edge_does_not_consume_next_hop_quota`.'
  labels:
  - covers:class-hierarchy-graph:3.2:3.2.1
  - covers:class-hierarchy-graph:3.2:3.2.2
  - covers:class-hierarchy-graph:3.2:3.2.3
  - covers:class-hierarchy-graph:3.2:3.2.4
  - covers:class-hierarchy-graph:3.2:3.2.5
  - covers:class-hierarchy-graph:3.2:3.2.6
  - covers:class-hierarchy-graph:3.2:3.2.7
  - covers:class-hierarchy-graph:3.2:3.2.8
  - covers:class-hierarchy-graph:3.2:3.2.9
  - covers:class-hierarchy-graph:3.2:3.2.10
  - covers:class-hierarchy-graph:3.2:3.2.11
  - covers:class-hierarchy-graph:3.2:3.2.12
  - covers:class-hierarchy-graph:3.2:3.2.13
  - covers:class-hierarchy-graph:3.2:3.2.14
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Add MCG view with Leiden communities
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  validation_criteria: "3.3.1: MCG uses scoped IMPORTS edges, not a project-wide sample.\
    \ symbol: `CodewikiFacts::scoped_edges`.\n3.3.2: Community ids come from `analyze`\
    \ / Leiden, not a new partitioner. symbol: `analyze`.\n3.3.3: A two-cluster fixture\
    \ yields two communities on the scoped subgraph. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_assigns_leiden_communities_on_scoped_imports`.\n\
    3.3.4: A unique file path and a uniquely resolving module name whose provider\
    \ is that file resolve to the same scoped node and edge set, including a consumer\u2192\
    provider\u2192dependency fixture whose incoming consumers and outgoing dependencies\
    \ appear for both seed forms. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_file_path_and_module_name_resolve_same_scope`.\n\
    3.3.5: A missing module-name seed and a module with zero or two-plus provider\
    \ files are user errors, not empty or guessed graphs. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_module_seed_rejects_missing_and_ambiguous`.\n\
    3.3.6: Omitted `--depth` on `--view=mcg` walks one hop and serializes `depth:\
    \ 1`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_omitted_depth_defaults_by_kind`.\n\
    3.3.7: A file-path seed equal to a different module's name does not traverse that\
    \ module's incoming IMPORTS; the walked node and edge set stays file-typed. test:\
    \ `crates/gcode/src/commands/graph/view/mcg.rs::mcg_file_seed_does_not_admit_same_named_module_imports`.\n\
    3.3.8: `--view=mcg` applies the 3.1 visibility-before-bound helper before every\
    \ `LIMIT` and hop expansion. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_applies_visible_owner_set_before_limits`.\n\
    3.3.9: A module imported by exactly one consumer file whose provider is a different\
    \ file canonicalizes to the provider, not the importer. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_module_seed_uses_provider_not_unique_importer`.\n\
    3.3.10: A cyclic IMPORTS fixture at `--depth > 2` expands each typed node once,\
    \ emits each public edge once, and stays deterministic. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_cycle_emits_unique_nodes_and_edges`.\n\
    3.3.11: A depth-2 incoming IMPORTS walk whose prior-hop public edge sorts before\
    \ a new neighbor still includes that neighbor at `incoming-limit=1` and does not\
    \ set `incoming_truncated` unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_prior_edge_does_not_consume_next_hop_quota`.\n\
    3.3.12: A depth-2 outgoing IMPORTS walk whose prior-hop public edge sorts before\
    \ a new neighbor still includes that neighbor at `outgoing-limit=1` and does not\
    \ set `outgoing_truncated` unless a new eligible overflow remains. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_prior_outgoing_edge_does_not_consume_next_hop_quota`.\n\
    3.3.13: A depth-2 IMPORTS fixture where incoming truncates at one executed frontier\
    \ and outgoing truncates at another keeps both top-level flags true and membership\
    \ deterministic. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_or_aggregates_asymmetric_truncation`.\n\
    3.3.14: A module seed and its unique provider-file seed both include consumers\
    \ targeting the module name and the provider's outgoing IMPORTS; a file-only frontier\
    \ is insufficient. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_module_and_provider_seeds_include_consumers_and_deps`.\n\
    3.3.15: Two raw aliases that uniquely resolve to one provider (a path-derived\
    \ name and an importer-relative name) plus the provider-file seed return identical\
    \ nodes, edges, and directional flags; the file seed's module-key set is the full\
    \ `E(P)`, not one guessed specifier. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_two_aliases_and_provider_file_share_equivalence_class`.\n\
    3.3.16: A depth-2 chain `P \u2192 Q \u2192 R` where `P` imports an alias of `Q`\
    \ and `Q` imports an alias of `R` yields the same second-hop nodes, edges, bounds,\
    \ and direction flags from `P`'s file seed and every uniquely resolving alias\
    \ of `P`; discovering the `Q` module closes through `Q`'s provider file and `E(Q)`\
    \ so `R` is not omitted. test: `crates/gcode/src/commands/graph/view/mcg.rs::mcg_depth_two_closes_discovered_frontier_equivalence`."
  labels:
  - covers:class-hierarchy-graph:3.3:3.3.1
  - covers:class-hierarchy-graph:3.3:3.3.2
  - covers:class-hierarchy-graph:3.3:3.3.3
  - covers:class-hierarchy-graph:3.3:3.3.4
  - covers:class-hierarchy-graph:3.3:3.3.5
  - covers:class-hierarchy-graph:3.3:3.3.6
  - covers:class-hierarchy-graph:3.3:3.3.7
  - covers:class-hierarchy-graph:3.3:3.3.8
  - covers:class-hierarchy-graph:3.3:3.3.9
  - covers:class-hierarchy-graph:3.3:3.3.10
  - covers:class-hierarchy-graph:3.3:3.3.11
  - covers:class-hierarchy-graph:3.3:3.3.12
  - covers:class-hierarchy-graph:3.3:3.3.13
  - covers:class-hierarchy-graph:3.3:3.3.14
  - covers:class-hierarchy-graph:3.3:3.3.15
  - covers:class-hierarchy-graph:3.3:3.3.16
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Add class-hierarchy view
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '2.2'
  validation_criteria: '3.4.1: `--view=class-hierarchy` returns the ancestor/descendant
    DAG through the dedicated hop walker, not `scoped_edges`. file: `crates/gcode/src/commands/graph/view/class_hierarchy.rs`.

    3.4.2: A same-file base/derived pair appears in both directions: outgoing from
    Derived is Base (ancestor); incoming to Base includes Derived (descendant). test:
    `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_includes_base_and_derived_both_directions`.

    3.4.3: Depth 1 omits the grandparent; default depth includes it. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_depth_caps_chain`.

    3.4.4: A diamond DAG emits each shared ancestor and each edge once; depth 1 from
    `D` omits `A` and sets the ancestor-side hop-cut flag; default depth includes
    `A` and clears that flag. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_diamond_is_complete_dag`.

    3.4.5: JSON and Mermaid distinguish `INHERITS`, `EXTENDS`, and `IMPLEMENTS` via
    `type(r)`. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_preserves_heritage_subtypes`.

    3.4.6: External and unresolved heritage targets appear as terminal nodes with
    stable ids and typed edges. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_includes_external_and_unresolved_terminals`.

    3.4.7: A hop larger than `MAX_DECLARED_EDGE_LIMIT` is fully consumed by pagination;
    the result is not silently capped. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_paginates_hop_to_exhaustion`.

    3.4.8: Pagination uses a total order `(source.id, target.id, type(r), id(r))`;
    tied endpoints with different heritage types and parallel edges neither skip nor
    duplicate across page boundaries. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_pagination_is_total_order`.

    3.4.9: Seeded at one child of a two-child base, the sibling is absent from nodes
    and edges. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_excludes_siblings`.

    3.4.10: Omitted `--depth` on `--view=class-hierarchy` uses 8 and serializes `depth:
    8`. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_omitted_depth_defaults_by_kind`.

    3.4.11: A local Trait seed returns an external or unresolved impl Type as a descendant
    terminal with a typed `IMPLEMENTS` edge. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_includes_external_and_unresolved_sources`.

    3.4.12: `--view=class-hierarchy` applies the 3.1 visibility-before-bound helper
    before each page-exhaustion and hop-cut decision. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_applies_visible_owner_set_before_hop_cut`.

    3.4.13: A base-seeded descendant chain at depth 1 sets `incoming_truncated=true`
    and `outgoing_truncated=false`; at sufficient depth the full descendant chain
    is present and `incoming_truncated` is false. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_descendant_depth_caps_incoming`.

    3.4.14: Two edges that share source, target, and type but differ by `id(r)` paginate
    without skip or duplicate; `edge_id` is used internally and is absent from the
    public payload. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_pagination_uses_internal_edge_id`.

    3.4.15: Two projects that share endpoint ids do not contaminate a CHG ancestor
    or descendant page; both endpoints are project-bound. test: `crates/gcode/src/commands/graph/view/class_hierarchy.rs::class_hierarchy_binds_project_on_both_endpoints`.'
  labels:
  - covers:class-hierarchy-graph:3.4:3.4.1
  - covers:class-hierarchy-graph:3.4:3.4.2
  - covers:class-hierarchy-graph:3.4:3.4.3
  - covers:class-hierarchy-graph:3.4:3.4.4
  - covers:class-hierarchy-graph:3.4:3.4.5
  - covers:class-hierarchy-graph:3.4:3.4.6
  - covers:class-hierarchy-graph:3.4:3.4.7
  - covers:class-hierarchy-graph:3.4:3.4.8
  - covers:class-hierarchy-graph:3.4:3.4.9
  - covers:class-hierarchy-graph:3.4:3.4.10
  - covers:class-hierarchy-graph:3.4:3.4.11
  - covers:class-hierarchy-graph:3.4:3.4.12
  - covers:class-hierarchy-graph:3.4:3.4.13
  - covers:class-hierarchy-graph:3.4:3.4.14
  - covers:class-hierarchy-graph:3.4:3.4.15
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Update CLI contract and code-index skill
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  - '3.3'
  - '3.4'
  validation_criteria: '3.5.1: CLI contract lists `callees` and `graph view` with
    `nodes`, `edges`, and the other payload keys above. symbol: `contract`.

    3.5.2: Both SKILL.md copies document the new commands. file: `crates/gcode/assets/SKILL.md`.

    3.5.3: Bundled manifest hash matches the install-shared skill. file: `src/gobby/install/bundled_content_manifest.json`.

    3.5.4: Generated contract tests assert the complete `graph_payload_keys` set including
    `project_id`, `project_root`, `hint`, `nodes`, and `edges`. test: `crates/gcode/tests/contract.rs::graph_view_payload_keys_are_complete`.

    3.5.5: After converting `#17680` to an epic and expanding, its description and
    `validation_criteria` name fcg, mcg, class-hierarchy, callees, Leiden-on-mcg,
    and complete Mermaid. behavior: "root task #17680 product bar matches this plan".

    3.5.6: Vendored `tests/contracts/gcode.contract.json` matches the crate contract
    for `callees` and `graph view`; `docs/contracts/gcode-cli.md` names both commands.
    file: `tests/contracts/gcode.contract.json`.

    3.5.7: `contract_version` advances from the current value (4 at this writing)
    by exactly one and is identical in `contract()`, `crates/gcode/contract/gcode.contract.json`,
    `tests/contracts/gcode.contract.json`, and `docs/contracts/gcode-cli.md`. symbol:
    `contract`.

    3.5.8: `gcode callees` and `gcode graph view` classify as read navigation; `gcode
    graph clear` does not. test: `tests/hooks/test_normalization.py::test_gcode_callees_and_graph_view_are_navigation`.

    3.5.9: A no-mutation tool-chat policy accepts `callees` and nested `graph view`
    and rejects `graph clear`. test: `tests/ai/test_tool_chat_tools.py::test_graph_view_is_readonly_without_graph_mutators`.

    3.5.10: The code-index skill tests document the new commands and still require
    the two SKILL.md copies to be byte-identical. test: `tests/skills/test_code_index_skill.py::test_code_index_skill_matches_gcode_bundled_asset_when_present`.'
  labels:
  - covers:class-hierarchy-graph:3.5:3.5.1
  - covers:class-hierarchy-graph:3.5:3.5.2
  - covers:class-hierarchy-graph:3.5:3.5.3
  - covers:class-hierarchy-graph:3.5:3.5.4
  - covers:class-hierarchy-graph:3.5:3.5.5
  - covers:class-hierarchy-graph:3.5:3.5.6
  - covers:class-hierarchy-graph:3.5:3.5.7
  - covers:class-hierarchy-graph:3.5:3.5.8
  - covers:class-hierarchy-graph:3.5:3.5.9
  - covers:class-hierarchy-graph:3.5:3.5.10
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
```
