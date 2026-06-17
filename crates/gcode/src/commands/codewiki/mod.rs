use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use crate::graph::typed_query;
use crate::index::hasher;
use crate::models::Symbol;

const DEFAULT_OUT_DIR: &str = "codewiki";
const CODEWIKI_META_PATH: &str = "_meta/codewiki.json";
const OWNERSHIP_META_PATH: &str = "_meta/ownership.json";
const MAX_MERMAID_HOPS: usize = 2;
const MAX_MERMAID_EDGES: usize = 20;
const MAX_EDGE_LIMIT: usize = 100_000;
const CODEWIKI_RENDER_VERSION: u32 = 4;

/// Default daemon feature profile for aggregate (module/repo/architecture)
/// prose, which synthesizes 10k+-token grounded prompts; file and symbol
/// docs stay on the daemon's default low-tier profile.
pub(crate) const DEFAULT_AGGREGATE_PROFILE: &str = "feature_mid";

mod build;
mod cluster;
mod generation;
mod graph;
mod io;
mod ownership;
mod paths;
mod progress;
mod prompts;
mod render;
mod reuse;
mod run;
mod text;
mod types;

// Document builders.
#[cfg(test)]
pub(crate) use build::build_module_docs;
pub(crate) use build::{
    FileDocPosition, build_architecture_doc, build_codewiki_changes_doc,
    build_codewiki_index_snapshot, build_curated_navigation_docs, build_file_doc,
    build_hotspots_doc, build_module_docs_with_filter, build_onboarding_doc,
};
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
    generate_hierarchical_docs_with_reuse,
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
// Rendered markdown and graph diagrams.
pub(crate) use render::{
    build_repo_doc, collect_subsystem_dependency_edges, render_architecture_doc,
    render_architecture_structure_mermaid, render_file_doc, render_hotspots_doc,
    render_module_call_mermaid, render_module_dependency_mermaid, render_module_doc,
    render_onboarding_doc, render_subsystem_dependency_mermaid,
};
// Reuse of unchanged docs without regeneration.
pub(crate) use reuse::{ReusePlan, span_files};
pub use run::run;
#[cfg(test)]
pub(crate) use run::{load_symbols_for_codewiki, should_document_file, validate_edge_limit};
// AI and structural text helpers.
pub(crate) use text::{
    Generation, append_relevant_source_files, citation_list, citation_markers, collect_link_spans,
    display_child_summary, frontmatter_with_degradation,
    frontmatter_with_degradation_without_ranges, ground_text, maybe_generate,
    neutralize_symbol_purpose_links, replace_citations_with_markers, resolve_text_generator,
    structural_file_summary, structural_module_summary, structural_repo_summary,
    structural_symbol_purpose, write_references, write_section,
};
#[cfg(test)]
pub(crate) use text::{frontmatter, generate_with_bounded_retry};
pub use types::{
    AiDepth, CodewikiAiOptions, CodewikiGraphAvailability, CodewikiGraphEdge,
    CodewikiGraphEdgeKind, CodewikiInput, CodewikiRunSummary, LeadingChunk, PromptTier,
    TextGenerator,
};
pub(crate) use types::{
    ArchitectureDoc, ArchitectureSubsystem, BuiltDoc, CodewikiDocMeta, CodewikiFileSnapshot,
    CodewikiGraph, CodewikiIndexSnapshot, CodewikiMeta, CodewikiSymbolSnapshot, FileDoc, FileLink,
    HotspotFinding, HotspotNode, HotspotsDoc, ModuleDoc, ModuleLink, OnboardingDoc,
    OnboardingEntryPoint, OnboardingStep, SourceSpan, SymbolDoc, ranked_source_excerpts,
    source_excerpt_for_file,
};

#[cfg(test)]
pub(crate) use io::write_incremental_doc_set_with_snapshot;
pub(crate) use io::{DocPruneScope, DocSink, read_ownership_meta, write_ownership_meta};
pub use io::{write_doc_set, write_incremental_doc_set};

#[cfg(test)]
mod tests;
