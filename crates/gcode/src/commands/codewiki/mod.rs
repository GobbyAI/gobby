use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::graph::typed_query;
use crate::index::hasher;
use crate::models::Symbol;

const DEFAULT_OUT_DIR: &str = "codewiki";
const CODEWIKI_META_PATH: &str = "_meta/codewiki.json";
const OWNERSHIP_META_PATH: &str = "_meta/ownership.json";
const MAX_EDGE_LIMIT: usize = 100_000;
/// Cache epoch for generated pages. Bumped 9 -> 10 so reused pages re-render
/// with compact navigation-table cells: a subsystem / child-module / module
/// summary inlined into a parent table cell is reduced to its leading paragraph
/// (the full multi-table brief stays on the module's own page) instead of a wall
/// of pipe-escaped pseudo-tables.
/// Bumped 8 -> 9 for the deep-wiki narrative
/// pass: file pages demote `## Key components` to a capped `## Reference` table
/// with test-gated symbols collapsed into a single behavior-spec line; the
/// architecture topology diagram stitches fixed required/degraded service-edge
/// labels; the page prompts demand design rationale, failure/degradation
/// behavior, and evaluator context; and the grounding pass no longer appends the
/// trailing bare-citation dump when a page already cites inline (#895). All
/// reused pages must regenerate into the new shape.
/// Bumped 7 -> 8 so reused pages regenerate
/// without the auto-generated mermaid code-graph diagrams (per-module
/// dependency/call diagrams, repo/architecture subsystem maps), which were the
/// sole source of `graph-truncated`/`graph-unavailable` page degradation; graph
/// availability is now informational only and never marks a page degraded.
/// Bumped 10 -> 11 so narrative chapters re-render under the deterministic,
/// reading-ordered slug scheme (`code/narrative/01-introduction.md`, #900): the
/// old alphabetical bare-slug pages become orphans that `finish`'s cache-
/// independent GC reclaims on this first full regen.
/// Bumped 6 -> 7 so verifier audit notes appear in frontmatter even when source
/// hashes are unchanged. Bumped 5 -> 6 so file and module pages written in the
/// old symbol-dump shape (API Symbols / Component ID / Lines table, full-range
/// `<details>` provenance) cannot be reused from disk: the new shape renders a
/// verified narrative body plus a human Key components table. (5 was the
/// grounded verification pass; 4 the pre-verify pages.)
// 14 (#966): generated Markdown is normalized at the shared formatter boundary
// so mutable codewiki pages refresh without MD012 blank-line violations.
// 13 (#905): curated concept/narrative pages strip a duplicate leading `# H1`
// the model writes at the top of its body (the renderer owns the canonical
// `# {title}`), so prior on-disk pages with two H1s re-render with one.
// 12 (#904): repo overview links the analysis/catalog pages (feature catalog,
// infrastructure, deprecations, dead-code candidates) from a new "Analysis &
// catalogs" section, narrative extra chapters carry readable `NN-<title>` slugs,
// and aggregate prose is written opus-first — so prior on-disk pages re-render.
// 16 (#976): tier -> feature profile is owned by gcore's `profile_for_tier`
// (Aggregate -> feature_high, Module -> feature_mid, Standard -> feature_low) and
// the Direct route honors per-tier profile targets, so cached pages written under
// the prior None-profile defaults re-render.
// 18 (#985): AI route/fallback/status frontmatter and metadata are explicit, and
// the rejected repo-level code-graph dependency diagram is absent again.
const CODEWIKI_RENDER_VERSION: u32 = 18;

/// Default daemon feature profile for the grounded verification pass (#904):
/// `feature_mid` (sonnet) runs the "is this claim supported by the cited
/// source?" QA judgment, pairing with the aggregate writer profile resolved in
/// `text/generation.rs`. File and symbol docs stay on the daemon's default
/// low-tier profile.
pub(crate) const DEFAULT_VERIFY_PROFILE: &str = "feature_mid";

mod architecture_diagrams;
mod build;
mod cluster;
mod generation;
mod graph;
mod io;
mod ownership;
mod paths;
mod progress;
mod prompts;
mod relationship_facts;
mod render;
mod repair;
mod reuse;
mod run;
mod system_model;
mod text;
mod truth_digest;
mod types;

// Document builders.
#[cfg(test)]
pub(crate) use build::build_module_docs;
pub(crate) use build::{
    AuditContext, FileDocPosition, build_architecture_doc, build_audit_context,
    build_codewiki_changes_doc, build_codewiki_index_snapshot, build_curated_navigation_docs,
    build_deprecations_doc, build_feature_catalog_doc, build_file_doc, build_hotspots_doc,
    build_infrastructure_doc, build_module_docs_with_filter, build_onboarding_doc,
};
pub(crate) use truth_digest::build_truth_digest;
// Module clustering and graph-to-file helpers.
pub(crate) use cluster::{
    cluster_file_modules, files_for_import_target, first_component_for_file,
    symbols_by_file_component,
};
#[cfg(test)]
pub(crate) use cluster::{common_module_for_files, find_file_root};
// Optional FalkorDB graph queries.
pub use generation::generate_hierarchical_docs;
#[cfg(test)]
pub(crate) use generation::{
    generate_hierarchical_docs_core, generate_hierarchical_docs_with_progress,
    generate_hierarchical_docs_with_reuse, generate_hierarchical_docs_with_verify,
};
pub(crate) use graph::fetch_codewiki_graph_edges;
#[cfg(test)]
pub(crate) use graph::{
    codewiki_call_edges_query, codewiki_import_edges_query, import_edges_from_pairs,
};
pub(crate) use ownership::{OwnershipMeta, OwnershipOptions, build_ownership_doc};
pub(crate) use progress::CodewikiProgress;
// Markdown path and wikilink helpers.
pub(crate) use paths::{
    component_label, direct_child_modules, file_doc_path, file_wikilink, in_scope, inline_code,
    is_core_file, module_ancestors, module_depth, module_doc_path, module_for_file,
    module_is_ancestor, module_wikilink, parent_module, plural, write_markdown_table_header,
    write_markdown_table_row,
};
// Cross-file relationship facts threaded into narrative prompts (#885).
pub(crate) use relationship_facts::{RelationshipFacts, relationship_facts_for_file};
// Deterministic, no-LLM workspace system model (#887, epic #886). Consumed by
// the architecture diagram leaf (#891) below to seed model-derived diagrams.
pub use system_model::{
    Crate, Edge, RuntimeMode, ServiceBoundary, ServiceKind, SystemModel, build_system_model,
};
// Model-seeded architectural Mermaid diagrams for the architecture page (#891).
#[cfg(test)]
pub(crate) use architecture_diagrams::is_valid_mermaid;
pub(crate) use architecture_diagrams::{render_architecture_diagrams, render_service_matrix};
// Rendered markdown and graph-derived narrative analysis.
pub(crate) use render::{
    build_repo_doc, collect_subsystem_dependency_edges, render_architecture_doc,
    render_deprecations_doc, render_feature_catalog_doc, render_file_doc, render_hotspots_doc,
    render_infrastructure_doc, render_module_doc, render_onboarding_doc,
};
// Reuse of unchanged docs without regeneration.
pub(crate) use reuse::{ReusePlan, span_files};
#[cfg(test)]
pub(crate) use run::{
    git_changed_files, load_symbols_for_codewiki, should_document_file, validate_edge_limit,
};
pub use run::{run, run_repair};
// Citation repair: re-anchor on-disk citations against the current index with
// no regeneration. Public so a later leaf's `--repair-citations` flag drives it.
pub use repair::{CitationRepairSummary, repair_citations};
// AI and structural text helpers.
pub(crate) use text::{
    CitationResolver, GenerationContent, GenerationOutcome, VerifyOutcome,
    append_curated_source_files, append_relevant_source_files, citation_list, citation_markers,
    collect_link_spans, display_child_summary, frontmatter_with_degradation,
    frontmatter_with_degradation_and_verify_notes_without_ranges,
    frontmatter_with_degradation_without_ranges, ground_text, is_ai_generation_failure_code,
    maybe_generate, neutralize_symbol_purpose_links, reanchor_citations,
    replace_citations_with_markers, resolve_text_generator, resolve_text_verifier,
    structural_file_summary, structural_module_summary, structural_repo_summary,
    structural_symbol_purpose, verify_with_notes, write_references, write_section,
};
#[cfg(test)]
pub(crate) use text::{frontmatter, generate_with_bounded_retry};
pub use types::{
    AiDepth, CodewikiAiOptions, CodewikiGraphAvailability, CodewikiGraphEdge,
    CodewikiGraphEdgeKind, CodewikiInput, CodewikiRunSummary, LeadingChunk, PromptTier, ProseDepth,
    ProseRegister, TextGenerator, TextVerifier,
};
pub(crate) use types::{
    AiGenerationStatus, ArchitectureDoc, ArchitectureSubsystem, BuiltDoc, CodewikiAiOutcome,
    CodewikiDocMeta, CodewikiFileSnapshot, CodewikiGraph, CodewikiIndexSnapshot, CodewikiMeta,
    CodewikiSymbolSnapshot, CodewikiTruthDigest, CodewikiTruthStackEntry, CodewikiTruthSuperseded,
    DeprecatedSymbol, DeprecationIndex, DeprecationsDoc, FeatureCatalogDoc, FileDoc, FileLink,
    HotspotFinding, HotspotNode, HotspotsDoc, InfraSection, InfrastructureDoc, ModuleDoc,
    ModuleLink, OnboardingDoc, OnboardingEntryPoint, OnboardingStep, SourceSpan, SymbolDoc,
    TestIndex, VerifyNote, ranked_source_excerpts, source_excerpt_for_file,
};
// Feature catalog row/section types (#888) are only named by the catalog's
// drift-guard tests; the lib builds the page through `FeatureCatalogDoc`.
#[cfg(test)]
pub(crate) use types::FeatureBinarySection;

#[cfg(test)]
pub(crate) use io::write_incremental_doc_set_with_snapshot;
pub(crate) use io::{DocPruneScope, DocSink, read_ownership_meta, write_ownership_meta};
pub use io::{write_doc_set, write_incremental_doc_set};
#[cfg(test)]
pub(crate) use truth_digest::TRUTH_DIGEST_META_PATH;
pub(crate) use truth_digest::write_truth_digest;

#[cfg(test)]
mod tests;
