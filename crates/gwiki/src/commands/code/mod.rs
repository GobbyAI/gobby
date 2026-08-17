use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use runtime::hasher;
use types::Symbol;

const DEFAULT_OUT_DIR: &str = "codewiki";
const CODEWIKI_META_PATH: &str = "_meta/codewiki.json";
const OWNERSHIP_META_PATH: &str = "_meta/ownership.json";
const MAX_EDGE_LIMIT: usize = 100_000;
// Per-category render versions (#1007): the single global version was replaced
// by per-category constants so a template change in one renderer only
// invalidates only affected pages. Epoch 20 is the baseline inherited from the
// former global render version.
const RENDER_VERSION_DEFAULT: u32 = 20;
const RENDER_VERSION_FILE: u32 = 20;
// 21 (#18570): module pages regenerate once with both restored deterministic
// dependency and depth-bearing call-sequence diagram sections.
const RENDER_VERSION_MODULE: u32 = 21;
const RENDER_VERSION_REPO: u32 = 20;
// Architecture and deterministic aggregate pages use epoch 21 for their
// current diagram/provenance shapes.
const RENDER_VERSION_ARCHITECTURE: u32 = 21;
const RENDER_VERSION_INFRASTRUCTURE: u32 = 21;
const RENDER_VERSION_FEATURES: u32 = 21;
const RENDER_VERSION_DEPRECATIONS: u32 = 21;
const RENDER_VERSION_MISC: u32 = 21;
// Curated handbook pages advance independently as their narrative and diagram
// contract changes.
const RENDER_VERSION_CURATED: u32 = 22;
const RENDER_VERSION_CHANGES: u32 = 21;

/// Returns the render-version constant for a doc page path. Each page category
/// (file docs, module docs, architecture, curated narrative, etc.) has its own
/// version so a template change in one renderer only invalidates the pages it
/// affects, instead of forcing a full wiki regeneration.
pub(crate) fn render_version_for_path(path: &str) -> u32 {
    if path.starts_with("code/files/") {
        RENDER_VERSION_FILE
    } else if path.starts_with("code/modules/") {
        RENDER_VERSION_MODULE
    } else if types::is_curated_doc(path) {
        RENDER_VERSION_CURATED
    } else if path == types::REPO_DOC_PATH {
        RENDER_VERSION_REPO
    } else if path == types::ARCHITECTURE_DOC_PATH {
        RENDER_VERSION_ARCHITECTURE
    } else if path == "code/infrastructure.md" {
        RENDER_VERSION_INFRASTRUCTURE
    } else if path == "code/features.md" {
        RENDER_VERSION_FEATURES
    } else if path == "code/deprecations.md" {
        RENDER_VERSION_DEPRECATIONS
    } else if path == "code/_changes.md" {
        RENDER_VERSION_CHANGES
    } else if path == "code/_onboarding.md"
        || path == "code/_hotspots.md"
        || path == "code/_ownership.md"
    {
        RENDER_VERSION_MISC
    } else {
        RENDER_VERSION_DEFAULT
    }
}

/// Default daemon feature profile for the grounded verification pass (#904):
/// `feature_mid` (sonnet) runs the "is this claim supported by the cited
/// source?" QA judgment, pairing with the aggregate writer profile resolved in
/// `text/generation.rs`. File and symbol docs stay on the daemon's default
/// low-tier profile.
pub(crate) const DEFAULT_VERIFY_PROFILE: &str = "feature_mid";

mod architecture_diagrams;
mod build;
mod cluster;
mod command;
mod compare;
mod diagram_compose;
mod doc_paths;
mod frontmatter;
mod generation;
mod graph;
mod io;
mod lock;
mod ownership;
mod paths;
mod progress;
mod prompts;
mod publication;
mod purge;
mod relationship_facts;
mod render;
mod repair;
mod reuse;
mod reuse_guard;
mod run;
mod runtime;
mod strict_markdown;
mod stubs;
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
    canonical_project_root, hash_snapshot_file_at_root, resolve_file_reuse,
    resolve_tool_loop_dump_dir,
};
pub(crate) use command::run_command;
pub use command::{CodeCommandOptions, DEFAULT_CODE_GRAPH_EDGE_LIMIT};
#[cfg(test)]
pub(crate) use lock::{CODE_WRITER_LOCK_RELATIVE_PATH, CODE_WRITER_LOCK_TIMEOUT};
pub(crate) use publication::{CodewikiPublication, PublicationFingerprint, code_wikilinks};
pub(crate) use reuse_guard::{
    file_module_link_key, module_child_links_key, nav_set_invalidation_key,
    restamp_file_module_link, reused_module_child_links_current,
};
pub(crate) use runtime::CodeEngineRuntime;
pub(crate) use truth_digest::build_truth_digest;
// Module clustering and graph-to-file helpers.
pub(crate) use cluster::{
    cluster_file_modules, files_for_import_target, first_component_for_file,
    symbols_by_file_component,
};
#[cfg(test)]
pub(crate) use cluster::{common_module_for_files, find_file_root};
// Optional FalkorDB graph queries.
#[cfg(test)]
pub(crate) use generation::{
    FileGenerationWorkers, GenerateDocsOptions, generate_hierarchical_docs,
};
pub(crate) use graph::fetch_codewiki_graph_edges;
#[cfg(test)]
pub(crate) use graph::import_edges_from_pairs;
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
#[cfg(test)]
pub(crate) use system_model::{Crate, Edge, RuntimeMode, ServiceBoundary};
pub(crate) use system_model::{ServiceKind, SystemModel, build_system_model};
// Model-seeded architectural diagram evidence for the architecture page
// (#891); since #17521 the diagrams themselves are LLM-composed from that
// evidence and edge-verified by `diagram_compose`. The Valid-Mermaid gate is
// the shared implementation in gobby-core (#17514); gwiki's lint consumes the
// same one, so generator and lint cannot drift.
pub(crate) use architecture_diagrams::{render_architecture_diagrams, render_service_matrix};
#[cfg(test)]
pub(crate) use compare::compare_to;
#[cfg(test)]
pub(crate) use gobby_core::vault::mermaid::is_valid_mermaid;
// Evidence-grounded LLM diagram composition (#17521): the model composes,
// deterministic code verifies every arrow against supplied evidence.
pub(crate) use diagram_compose::{
    DiagramEvidence, DiagramKind, DiagramOutcome, DiagramStats, NodeShape, compose_flowchart,
};
// Rendered markdown and graph-derived narrative analysis.
pub(crate) use render::{
    build_repo_doc, collect_subsystem_dependency_edges, module_diagram_context,
    render_architecture_doc, render_deprecations_doc, render_feature_catalog_doc, render_file_doc,
    render_hotspots_doc, render_infrastructure_doc, render_module_call_sequence_with_context,
    render_module_dependency_mermaid_with_context, render_module_doc, render_onboarding_doc,
};
#[cfg(test)]
pub(crate) use render::{render_module_call_sequence, render_module_dependency_mermaid};
// Reuse of unchanged docs without regeneration.
#[cfg(test)]
pub(crate) use purge::purge_generated_output;
pub(crate) use reuse::{ReusePlan, span_files};
#[cfg(test)]
pub(crate) use run::{
    git_changed_files, load_symbols_for_codewiki, should_document_file, validate_edge_limit,
};
// Citation repair: re-anchor on-disk citations against the current index with
// no regeneration. Public so a later leaf's `--repair-citations` flag drives it.
pub use repair::{CitationRepairSummary, repair_citations};
// AI and structural text helpers.
#[cfg(test)]
pub(crate) use text::ToolLoopResult;
pub(crate) use text::{
    CitationResolver, FrontmatterToolLoop, GRAPH_UNAVAILABLE, GenerationContent,
    GenerationObservability, GenerationOutcome, LANE_ONE_SHOT, LANE_TOOL_LOOP,
    MAX_FRONTMATTER_PROVENANCE_FILES, ToolLoopGenerator, VerifyOutcome,
    append_curated_source_files, append_relevant_source_files, citation_list, citation_markers,
    collect_link_spans, direct_route_candidate_error, display_child_summary,
    frontmatter_aggregate_with_verify_notes, frontmatter_aggregate_without_ranges,
    frontmatter_with_degradation, frontmatter_with_degradation_and_verify_notes_without_ranges,
    frontmatter_with_degradation_without_ranges, generate_aggregate, ground_text,
    is_ai_generation_failure_code, maybe_generate, neutralize_symbol_purpose_links,
    reanchor_citations, replace_citations_with_markers, resolve_text_generator,
    resolve_text_verifier, resolve_tool_loop_generator, structural_file_summary,
    structural_module_summary, structural_repo_summary, structural_symbol_purpose,
    verify_with_notes, write_references, write_section,
};
#[cfg(test)]
pub(crate) use text::{frontmatter, generate_with_bounded_retry};
pub use types::{
    AiDepth, CodewikiGraphAvailability, CodewikiGraphEdge, CodewikiGraphEdgeKind, CodewikiInput,
    CodewikiRunSummary, LeadingChunk, PromptTier, ProseDepth, ProseRegister, TextGenerator,
    TextVerifier, VerifyScope,
};
pub(crate) use types::{
    AiGenerationSettings, AiGenerationStatus, ArchitectureDoc, ArchitectureSubsystem, BuiltDoc,
    CodewikiAiOptions, CodewikiAiOutcome, CodewikiDocMeta, CodewikiFileSnapshot, CodewikiGraph,
    CodewikiIndexSnapshot, CodewikiMeta, CodewikiSymbolSnapshot, CodewikiTruthDigest,
    CodewikiTruthStackEntry, CodewikiTruthSuperseded, CommitStamp, DeprecatedSymbol,
    DeprecationIndex, DeprecationsDoc, FeatureCatalogDoc, FileDoc, FileLink, HotspotFinding,
    HotspotNode, HotspotsDoc, InfraSection, InfrastructureDoc, ModuleDoc, ModuleLink,
    OnboardingDoc, OnboardingEntryPoint, OnboardingStep, SourceSpan, SymbolDoc, SyncTextGenerator,
    SyncTextVerifier, TestIndex, VerifyNote, ranked_source_excerpts, source_excerpt_for_file,
};
// Feature catalog row/section types (#888) are only named by the catalog's
// drift-guard tests; the lib builds the page through `FeatureCatalogDoc`.
#[cfg(test)]
pub(crate) use types::FeatureBinarySection;

#[cfg(test)]
pub(crate) use frontmatter::page_frontmatter_blocks_reuse;
#[cfg(test)]
pub(crate) use io::write_incremental_doc_set_with_snapshot;
pub(crate) use io::{
    DocPruneScope, DocSink, content_sensitive_invalidation_key, read_ownership_meta,
    write_ownership_meta,
};
#[cfg(test)]
pub(crate) use io::{write_doc_set, write_incremental_doc_set};
#[cfg(test)]
pub(crate) use truth_digest::TRUTH_DIGEST_META_PATH;
pub(crate) use truth_digest::write_truth_digest;

#[cfg(test)]
mod tests;
