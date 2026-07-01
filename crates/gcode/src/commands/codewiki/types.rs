use std::collections::{BTreeMap, BTreeSet};

use gobby_core::config::AiRouting;
use serde::{Deserialize, Serialize};

use crate::models::Symbol;

use super::GenerationObservability;
use super::prompts;

#[derive(Debug, Clone)]
pub struct CodewikiInput {
    pub files: Vec<String>,
    pub graph_edges: Vec<CodewikiGraphEdge>,
    pub graph_availability: CodewikiGraphAvailability,
    pub symbols: Vec<Symbol>,
    /// Leading content chunk per file, retrieved from the code index. Feeds
    /// real source content into aggregate prompts and gives non-code files
    /// (markdown, config) a content-derived purpose. Missing entries degrade
    /// to summary-only prompts.
    pub leading_chunks: BTreeMap<String, LeadingChunk>,
}

/// The first indexed content chunk of a file: real source text with its
/// line range, used as retrieved prompt input and citation provenance.
#[derive(Debug, Clone)]
pub struct LeadingChunk {
    pub content: String,
    pub line_start: usize,
    pub line_end: usize,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub(crate) struct CodewikiTruthDigest {
    pub(crate) schema_version: u8,
    pub(crate) generated_at: String,
    pub(crate) project_id: String,
    pub(crate) repo_summary: String,
    pub(crate) stack_authority: String,
    pub(crate) stack: Vec<CodewikiTruthStackEntry>,
    pub(crate) key_paths: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) superseded: Vec<CodewikiTruthSuperseded>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub(crate) struct CodewikiTruthStackEntry {
    pub(crate) service: String,
    pub(crate) kind: String,
    pub(crate) adapter_module: String,
    pub(crate) pulled_in_by: Vec<String>,
    pub(crate) summary: String,
    pub(crate) degradation: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub(crate) struct CodewikiTruthSuperseded {
    pub(crate) old: String,
    pub(crate) new: String,
}

/// Builds a prompt source excerpt for `file` from its leading chunk.
pub(crate) fn source_excerpt_for_file(
    file: &str,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
) -> Option<prompts::SourceExcerpt> {
    leading_chunks
        .get(file)
        .map(|chunk| prompts::SourceExcerpt {
            path: file.to_string(),
            line_start: chunk.line_start,
            line_end: chunk.line_end,
            excerpt: chunk.content.clone(),
        })
}

/// Top-k source excerpts for a set of candidate file docs, ranked by symbol
/// count (the busiest files describe the module best) with path order as the
/// deterministic tie-break.
pub(crate) fn ranked_source_excerpts<'a>(
    candidates: impl Iterator<Item = &'a FileDoc>,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    limit: usize,
) -> Vec<prompts::SourceExcerpt> {
    let mut ranked = candidates.collect::<Vec<_>>();
    ranked.sort_by_key(|file| (std::cmp::Reverse(file.symbols.len()), file.path.clone()));
    ranked
        .into_iter()
        .filter_map(|file| source_excerpt_for_file(&file.path, leading_chunks))
        .take(limit)
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodewikiGraphEdge {
    pub source_component_id: String,
    pub target_component_id: String,
    pub kind: CodewikiGraphEdgeKind,
}

impl CodewikiGraphEdge {
    pub fn call(
        source_component_id: impl Into<String>,
        target_component_id: impl Into<String>,
    ) -> Self {
        Self {
            source_component_id: source_component_id.into(),
            target_component_id: target_component_id.into(),
            kind: CodewikiGraphEdgeKind::Call,
        }
    }

    pub fn import(
        source_component_id: impl Into<String>,
        target_component_id: impl Into<String>,
    ) -> Self {
        Self {
            source_component_id: source_component_id.into(),
            target_component_id: target_component_id.into(),
            kind: CodewikiGraphEdgeKind::Import,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CodewikiGraphEdgeKind {
    Call,
    Import,
}

#[derive(Debug, Clone)]
pub(crate) struct CodewikiGraph {
    pub(crate) edges: Vec<CodewikiGraphEdge>,
    pub(crate) availability: CodewikiGraphAvailability,
}

impl CodewikiGraph {
    pub(crate) fn available(edges: Vec<CodewikiGraphEdge>) -> Self {
        Self {
            edges,
            availability: CodewikiGraphAvailability::Available,
        }
    }

    pub(crate) fn truncated(edges: Vec<CodewikiGraphEdge>) -> Self {
        Self {
            edges,
            availability: CodewikiGraphAvailability::Truncated,
        }
    }

    pub(crate) fn unavailable() -> Self {
        Self {
            edges: Vec::new(),
            availability: CodewikiGraphAvailability::Unavailable,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CodewikiGraphAvailability {
    Available,
    Truncated,
    Unavailable,
}

#[derive(Debug, Clone)]
pub(crate) struct FileDoc {
    pub(crate) path: String,
    pub(crate) module: String,
    /// One-line file purpose for parent module/repo prompts and index listings.
    pub(crate) summary: String,
    /// The verified multi-section narrative body (`## Overview` + `## How it
    /// fits`) rendered on the file page; the Key components table is appended by
    /// the renderer. Empty when the doc was reused (the on-disk page is emitted
    /// verbatim via `reused_page`).
    pub(crate) body: String,
    pub(crate) source_spans: Vec<SourceSpan>,
    pub(crate) symbols: Vec<SymbolDoc>,
    pub(crate) component_ids: Vec<String>,
    /// True when AI generation was attempted for this doc and failed.
    pub(crate) degraded: bool,
    pub(crate) degraded_sources: Vec<String>,
    pub(crate) verify_notes: Vec<VerifyNote>,
    /// The on-disk page when the doc was reused without regeneration (#681);
    /// emitting disk content verbatim keeps a forced rewrite lossless.
    pub(crate) reused_page: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct SymbolDoc {
    pub(crate) symbol: Symbol,
    pub(crate) purpose: String,
    pub(crate) component_id: String,
    pub(crate) component_label: String,
    pub(crate) source_span: SourceSpan,
    /// Deprecation reason for this symbol, detected by the codewiki source scan
    /// (#889): `Some(reason)` when a `#[deprecated]` attribute or a `DEPRECATED`
    /// doc-comment sits above its definition (or in its docstring). Drives the
    /// visible "deprecated" badge in the file page's `## Key components` row.
    /// `None` for the common, non-deprecated case. Deterministic, never
    /// degrading.
    pub(crate) deprecation: Option<String>,
    /// Whether this symbol is test-gated (a `#[test]`/`#[cfg(test)]` attribute
    /// above it, or a tests path), detected by the same deterministic source
    /// scan that powers the dead-code page. The file page collapses test-gated
    /// symbols into a single behavior-spec line + count instead of one
    /// `## Reference` row each, so the readable surface is real code, not a test
    /// roster. `false` for the common case and for the AI-off/test entry points
    /// that pass no test index.
    pub(crate) is_test: bool,
}

#[derive(Debug, Clone)]
pub(crate) struct ModuleDoc {
    pub(crate) module: String,
    pub(crate) summary: String,
    pub(crate) source_spans: Vec<SourceSpan>,
    pub(crate) direct_files: Vec<FileLink>,
    pub(crate) child_modules: Vec<ModuleLink>,
    /// True when AI generation was attempted for this doc and failed.
    pub(crate) degraded: bool,
    pub(crate) degraded_sources: Vec<String>,
    pub(crate) verify_notes: Vec<VerifyNote>,
    /// The on-disk page when the doc was reused without regeneration (#681);
    /// emitting disk content verbatim keeps a forced rewrite lossless.
    pub(crate) reused_page: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ArchitectureDoc {
    pub(crate) source_spans: Vec<SourceSpan>,
    pub(crate) subsystems: Vec<ArchitectureSubsystem>,
    pub(crate) narrative: Option<String>,
    /// Pre-rendered, already-validated architectural Mermaid diagram section
    /// seeded from the deterministic workspace [`super::SystemModel`] (#891).
    /// `None` when no model was supplied or the model was too sparse to draw —
    /// a missing diagram is normal and never marks the page degraded.
    pub(crate) diagrams: Option<String>,
    /// Pre-rendered, deterministic service matrix seeded from the same
    /// [`super::SystemModel`] as `diagrams`: one row per service boundary with a
    /// fixed required/degraded requirement classification and what pulls it in.
    /// Gives an evaluator the at-a-glance "what does this need to run" picture;
    /// the narrative is asked to narrate around it. `None` when no model was
    /// supplied or it reached no services.
    pub(crate) service_matrix: Option<String>,
    pub(crate) degraded_sources: Vec<String>,
    /// Generation lane (`tool_loop` / `one_shot`) for the page's aggregate
    /// prose, recorded into frontmatter (#978).
    pub(crate) lane: &'static str,
    /// Accumulated Lane B tool-loop observability across the subsystem and
    /// narrative generations, recorded into frontmatter when `lane` is
    /// `tool_loop`.
    pub(crate) observability: GenerationObservability,
}

#[derive(Debug, Clone)]
pub(crate) struct ArchitectureSubsystem {
    pub(crate) module: String,
    pub(crate) responsibility: String,
    pub(crate) child_modules: Vec<String>,
    pub(crate) source_spans: Vec<SourceSpan>,
}

/// One infrastructure boundary on the deterministic infra-stack page (#892):
/// what the service is, what pulls it in, the adapter module that talks to it,
/// and how the workspace behaves when it is unavailable. Built straight from a
/// [`super::ServiceBoundary`] plus a curated descriptor — no LLM, never
/// degrading.
#[derive(Debug, Clone)]
pub(crate) struct InfraSection {
    pub(crate) service: String,
    pub(crate) pulled_in_by: Vec<String>,
    pub(crate) adapter_module: String,
    pub(crate) summary: String,
    pub(crate) degradation: String,
}

/// The deterministic infra-stack page (#892), one [`InfraSection`] per service
/// boundary in the system model. `degraded_sources` is always empty: the page
/// is derived from Cargo manifests + service boundaries and never marks itself
/// degraded.
#[derive(Debug, Clone)]
pub(crate) struct InfrastructureDoc {
    pub(crate) sections: Vec<InfraSection>,
    pub(crate) degraded_sources: Vec<String>,
}

/// One CLI subcommand row on the deterministic feature catalog page (#888):
/// the contract command name, its contract summary, the contract flag names,
/// a representative handler entry symbol, and the repo-relative handler file
/// the catalog wikilinks to as the explaining page. Built straight from the
/// pinned CLI contract JSON plus a curated dispatch resolver — no LLM.
#[derive(Debug, Clone)]
pub(crate) struct FeatureEntry {
    pub(crate) command: String,
    pub(crate) summary: String,
    pub(crate) key_flags: Vec<String>,
    pub(crate) entry_symbol: String,
    pub(crate) handler_file: String,
}

/// One binary's section on the feature catalog page: every subcommand the
/// binary's pinned contract declares, in contract order.
#[derive(Debug, Clone)]
pub(crate) struct FeatureBinarySection {
    pub(crate) binary: String,
    pub(crate) entries: Vec<FeatureEntry>,
}

/// The deterministic feature catalog page (#888), one [`FeatureBinarySection`]
/// per binary with a pinned contract. `degraded_sources` is always empty: the
/// page is derived from the contract JSONs + dispatch wiring and never marks
/// itself degraded.
#[derive(Debug, Clone)]
pub(crate) struct FeatureCatalogDoc {
    pub(crate) sections: Vec<FeatureBinarySection>,
    pub(crate) degraded_sources: Vec<String>,
}

/// Map of `symbol.id -> deprecation reason`, built once per run by the
/// deterministic source scan (#889) and threaded into `build_file_doc` (to
/// stamp the per-symbol badge) and the `code/deprecations.md` aggregate page.
/// A `BTreeMap` so the aggregate page lists symbols in a stable order. Empty
/// when nothing is deprecated; the scan never panics and never degrades.
pub(crate) type DeprecationIndex = BTreeMap<String, String>;

/// Set of `symbol.id`s that are test-gated, built by the same deterministic
/// source scan as [`DeprecationIndex`] and threaded into `build_file_doc` to
/// stamp `SymbolDoc::is_test`. A `BTreeSet` for stable, de-duplicated membership
/// checks. Empty when nothing is test-gated; the scan never panics or degrades.
pub(crate) type TestIndex = BTreeSet<String>;

/// One deprecated symbol on the deterministic `code/deprecations.md` page
/// (#889): its name, kind, defining `file:line`, the detected reason, and the
/// file it lives in (for grouping + a `file_wikilink`).
#[derive(Debug, Clone)]
pub(crate) struct DeprecatedSymbol {
    pub(crate) file: String,
    pub(crate) name: String,
    pub(crate) kind: String,
    pub(crate) line: usize,
    pub(crate) reason: String,
}

/// The deterministic deprecations aggregate page (#889), every deprecated
/// symbol grouped by file. `degraded_sources` is always empty: the page is
/// derived from a source scan and never marks itself degraded — even when the
/// list is empty (it still renders a clear "no deprecations" line).
#[derive(Debug, Clone)]
pub(crate) struct DeprecationsDoc {
    pub(crate) symbols: Vec<DeprecatedSymbol>,
    pub(crate) degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct OnboardingDoc {
    pub(crate) source_spans: Vec<SourceSpan>,
    pub(crate) entry_points: Vec<OnboardingEntryPoint>,
    pub(crate) reading_order: Vec<OnboardingStep>,
    pub(crate) degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct OnboardingEntryPoint {
    pub(crate) link: String,
    pub(crate) description: String,
    pub(crate) source_span: SourceSpan,
}

#[derive(Debug, Clone)]
pub(crate) struct OnboardingStep {
    pub(crate) module: String,
    pub(crate) summary: String,
    pub(crate) degree: usize,
    pub(crate) score: f64,
}

#[derive(Debug, Clone)]
pub(crate) struct HotspotsDoc {
    pub(crate) source_spans: Vec<SourceSpan>,
    pub(crate) hotspots: Vec<HotspotFinding>,
    pub(crate) god_nodes: Vec<HotspotFinding>,
    pub(crate) bridges: Vec<HotspotFinding>,
    pub(crate) degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct HotspotFinding {
    pub(crate) node: HotspotNode,
    pub(crate) degree: Option<usize>,
    pub(crate) score: Option<f64>,
    pub(crate) frequency: Option<usize>,
    pub(crate) weight: Option<f64>,
}

#[derive(Debug, Clone)]
pub(crate) struct HotspotNode {
    pub(crate) id: String,
    pub(crate) kind: String,
    pub(crate) label: String,
    pub(crate) wikilink: String,
    pub(crate) file_wikilink: Option<String>,
    pub(crate) source_span: Option<SourceSpan>,
}

#[derive(Debug, Clone)]
pub(crate) struct FileLink {
    pub(crate) path: String,
    pub(crate) summary: String,
    pub(crate) source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone)]
pub(crate) struct ModuleLink {
    pub(crate) module: String,
    pub(crate) summary: String,
    pub(crate) source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) struct SourceSpan {
    pub(crate) file: String,
    pub(crate) line_start: usize,
    pub(crate) line_end: usize,
}

const VERIFY_NOTE_REASON_LIMIT: usize = 200;
const VERIFY_NOTE_TRUNCATION: &str = "...";

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct VerifyNote {
    pub(crate) id: usize,
    pub(crate) reason: String,
}

impl VerifyNote {
    pub(crate) fn new(id: usize, reason: impl AsRef<str>) -> Self {
        Self {
            id,
            reason: normalize_verify_note_reason(reason.as_ref()),
        }
    }
}

fn normalize_verify_note_reason(reason: &str) -> String {
    let reason = reason.trim();
    if reason.chars().count() <= VERIFY_NOTE_REASON_LIMIT {
        return reason.to_string();
    }

    let keep = VERIFY_NOTE_REASON_LIMIT.saturating_sub(VERIFY_NOTE_TRUNCATION.len());
    let mut truncated = reason.chars().take(keep).collect::<String>();
    truncated.push_str(VERIFY_NOTE_TRUNCATION);
    truncated
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(crate) enum AiGenerationStatus {
    Generated,
    Degraded,
    Skipped,
}

impl AiGenerationStatus {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Generated => "generated",
            Self::Degraded => "degraded",
            Self::Skipped => "skipped",
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(crate) struct CodewikiAiOutcome {
    pub(crate) route: AiRouting,
    pub(crate) fallback: bool,
    pub(crate) status: AiGenerationStatus,
}

impl CodewikiAiOutcome {
    pub(crate) fn skipped(route: AiRouting, fallback: bool) -> Self {
        Self {
            route,
            fallback,
            status: AiGenerationStatus::Skipped,
        }
    }

    pub(crate) fn generated(route: AiRouting, fallback: bool) -> Self {
        Self {
            route,
            fallback,
            status: AiGenerationStatus::Generated,
        }
    }

    pub(crate) fn for_doc(self, degraded: bool) -> Self {
        if degraded {
            Self {
                status: AiGenerationStatus::Degraded,
                ..self
            }
        } else {
            self
        }
    }

    pub(crate) fn route_label(self) -> &'static str {
        ai_route_label(self.route)
    }
}

impl Default for CodewikiAiOutcome {
    fn default() -> Self {
        Self::skipped(AiRouting::Off, false)
    }
}

pub(crate) fn ai_route_label(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Auto | AiRouting::Off => "off",
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CodewikiRunSummary {
    pub command: &'static str,
    pub project_id: String,
    pub project_root: String,
    pub out_dir: String,
    pub generated_pages: usize,
    pub changed_paths: Vec<String>,
    pub skipped: usize,
    pub files: usize,
    pub modules: usize,
    pub symbols: usize,
    pub ai_enabled: bool,
    /// Pages whose AI content pass failed and fell back to the structural body
    /// this run (#900). Empty on a fully healthy run. Surfaced here (and logged
    /// to stderr) so curated/page degradation is visible instead of silently
    /// cached as healthy.
    pub degraded_pages: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub(crate) struct CodewikiMeta {
    pub(crate) docs: BTreeMap<String, CodewikiDocMeta>,
    pub(crate) generated_docs: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) index_snapshot: Option<CodewikiIndexSnapshot>,
    #[serde(default)]
    pub(crate) ai_mode: String,
}

#[derive(Debug, Clone, Default, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiDocMeta {
    pub(crate) source_hashes: BTreeMap<String, String>,
    /// True when the doc on disk was written from a failed generation
    /// fallback. Source hashes cannot see generation failures, so this flag
    /// is what lets a later successful run repair the doc (#687).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub(crate) degraded: bool,
    /// The grounded summary this doc feeds into parent prompts and pages,
    /// recorded so an unchanged doc can be reused without an LLM call (#681).
    /// Absent for degraded fallbacks and for docs nothing consumes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) summary: Option<String>,
    /// AI mode the doc on disk was generated under. Entries written before
    /// per-doc modes existed inherit the run-level `ai_mode` at read time.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) ai_mode: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) ai_route: String,
    #[serde(default)]
    pub(crate) ai_fallback: bool,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) ai_generation_status: String,
    /// Render-template version for deterministic markdown emitted after model
    /// generation. Per-category: each page type (file, module, architecture,
    /// curated, etc.) has its own version constant so a template change in one
    /// renderer only invalidates that category's pages (#1007). Missing or
    /// stale versions force a rewrite of the affected category only.
    #[serde(default)]
    pub(crate) render_version: u32,
    /// Cross-file neighbor source hashes (#885, Leaf H). A source-file page
    /// regenerates when a neighbor's content hash changes even if its own
    /// sources did not, so a caller edit refreshes the callee's relationship
    /// narrative. Empty for pages with no recorded cross-file neighbors.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub(crate) neighbor_hashes: BTreeMap<String, String>,
    /// Page-type invalidation digest for derived aggregate pages whose content
    /// is a function of a model rather than a source-file set (Leaf H): the
    /// `SystemModel` hash for architecture/infrastructure, and the rendered
    /// contract/deprecation digest for the feature catalog and audit pages. A
    /// function-body edit that does not change the model leaves the digest —
    /// and the page — unchanged. `None` for source-file pages.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) invalidation_key: Option<String>,
    /// Lane B tool-loop observability mirrored from the page frontmatter for an
    /// aggregate page produced by the tool loop (#978): the generation lane and
    /// the loop's call/turn counts. `None` for Lane A / leaf / deterministic
    /// pages. Recorded for traceability; not part of the reuse-invalidation
    /// comparison.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) lane: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) tool_call_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) turns: Option<usize>,
}

/// One rendered doc plus the degradation outcome of its generation, carried
/// to the incremental writer so `_meta/codewiki.json` can record it.
#[derive(Debug, Clone)]
pub(crate) struct BuiltDoc {
    pub(crate) path: String,
    pub(crate) content: String,
    pub(crate) degraded: bool,
    /// Grounded summary persisted to the doc meta so a later run can feed it
    /// into parent prompts without regenerating this doc (#681).
    pub(crate) summary: Option<String>,
    /// Cross-file neighbor files whose content this page's narrative depends on
    /// (#885, Leaf H). The sink hashes them into `neighbor_hashes` so a
    /// neighbor change invalidates this page even when its own sources are
    /// unchanged. Empty for pages with no cross-file dependencies.
    pub(crate) neighbors: BTreeSet<String>,
    /// Page-type invalidation digest for derived aggregate pages (Leaf H).
    /// `None` for source-file pages that invalidate on source/neighbor hashes.
    pub(crate) invalidation_key: Option<String>,
    /// True for keyed pages whose digest covers non-source inputs but whose
    /// provenance source hashes must still match before reuse.
    pub(crate) invalidation_key_requires_sources: bool,
}

impl BuiltDoc {
    pub(crate) fn healthy(path: impl Into<String>, content: String) -> Self {
        Self {
            path: path.into(),
            content,
            degraded: false,
            summary: None,
            neighbors: BTreeSet::new(),
            invalidation_key: None,
            invalidation_key_requires_sources: false,
        }
    }

    pub(crate) fn with_normalized_markdown(mut self) -> Self {
        if self.path.ends_with(".md") {
            self.content = gobby_core::markdown::normalize_markdown(&self.content);
        }
        self
    }

    /// A deterministic derived page (architecture, infrastructure, feature
    /// catalog, audit) keyed on `invalidation_key` rather than a source-file
    /// set: it is rewritten only when the digest changes (Leaf H).
    pub(crate) fn derived(
        path: impl Into<String>,
        content: String,
        invalidation_key: String,
    ) -> Self {
        Self {
            path: path.into(),
            content,
            degraded: false,
            summary: None,
            neighbors: BTreeSet::new(),
            invalidation_key: Some(invalidation_key),
            invalidation_key_requires_sources: false,
        }
    }

    pub(crate) fn with_source_sensitive_key(mut self) -> Self {
        self.invalidation_key_requires_sources = true;
        self
    }

    /// Records the cross-file neighbor files this page depends on, builder-style.
    pub(crate) fn with_neighbors(mut self, neighbors: BTreeSet<String>) -> Self {
        self.neighbors = neighbors;
        self
    }
}

#[derive(Debug, Clone, Default, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiIndexSnapshot {
    pub(crate) files: BTreeMap<String, CodewikiFileSnapshot>,
    pub(crate) symbols: BTreeMap<String, CodewikiSymbolSnapshot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) graph_neighborhoods: Option<BTreeMap<String, String>>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) degraded_sources: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiFileSnapshot {
    pub(crate) content_hash: String,
    pub(crate) symbol_count: usize,
}

#[derive(Debug, Clone, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiSymbolSnapshot {
    pub(crate) file_path: String,
    pub(crate) name: String,
    pub(crate) qualified_name: String,
    pub(crate) kind: String,
    pub(crate) line_start: usize,
}

pub type TextGenerator<'a> = dyn FnMut(&str, &str, PromptTier) -> Option<String> + 'a;

/// Grounded verification call: given a verify prompt and system prompt, returns
/// the raw model response, or `None` when the verifier is unavailable (routed
/// off, transport failure, or generation error). Callers treat `None` as "skip
/// verification, proceed undegraded". The deterministic block numbering,
/// response parsing, and stripping live in [`super::text`], so the closure is
/// just the model call — mirroring [`TextGenerator`] but without a prompt tier.
pub type TextVerifier<'a> = dyn FnMut(&str, &str) -> Option<String> + 'a;

/// Weight tier of one codewiki generation call (#904). `Aggregate` is the
/// top-level repo-wide synthesis — repo overview, architecture, and the curated
/// narrative/concept layer — and is written opus-first. `Module` is mid-level
/// per-unit synthesis (module docs and file-body narratives) and routes to
/// sonnet. `Standard` is high-volume per-symbol prose on the default low tier.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum PromptTier {
    #[default]
    Standard,
    Module,
    Aggregate,
}

/// How deep AI prose generation reaches. Deeper tiers include shallower ones;
/// gated tiers fall back to structural summaries.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum AiDepth {
    /// Architecture, module, and repo prose only.
    Sections,
    /// Sections plus per-file summaries.
    #[default]
    Files,
    /// Files plus per-symbol purposes (one LLM call per symbol).
    Symbols,
}

impl AiDepth {
    pub(crate) fn includes_files(self) -> bool {
        self >= AiDepth::Files
    }

    pub(crate) fn includes_symbols(self) -> bool {
        self >= AiDepth::Symbols
    }

    pub(crate) fn mode_label(self) -> &'static str {
        match self {
            AiDepth::Sections => "sections",
            AiDepth::Files => "files",
            AiDepth::Symbols => "symbols",
        }
    }
}

/// Output verbosity for AI prose, orthogonal to [`AiDepth`] (which page tiers
/// reach the LLM) and to the audience register. Maps to a per-page output token
/// budget; [`ProseDepth::Standard`] defers to the provider/profile default so a
/// run without the flag is byte-identical to before this control existed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum ProseDepth {
    /// Tighter pages: cap output low so prose stays terse.
    Brief,
    /// Provider/profile default budget (unchanged behavior).
    #[default]
    Standard,
    /// Richer pages: raise the output budget for longer explanations.
    Deep,
}

impl ProseDepth {
    /// Per-page output token budget, or `None` to defer to the provider/profile
    /// default. `Standard` returns `None` so the default run is unchanged;
    /// `Brief`/`Deep` pin a lower/higher ceiling.
    pub(crate) fn max_tokens(self) -> Option<usize> {
        match self {
            ProseDepth::Brief => Some(640),
            ProseDepth::Standard => None,
            ProseDepth::Deep => Some(2_400),
        }
    }
}

/// Audience register for AI prose, orthogonal to depth. Every register projects
/// the same grounded facts and only changes voice; `None` (the default) leaves
/// the base system prompts untouched so default runs are unchanged.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProseRegister {
    /// ELI5: plain language, defines jargon on first use, leads with the
    /// problem the code solves.
    Newcomer,
    /// Maintainer: leads with why the code is shaped this way and the
    /// non-obvious trade-offs.
    Maintainer,
    /// Build substrate: terse decisions and structure, minimal connective prose.
    Agent,
}

/// Which pages run the grounded verification pass. The aggregate/curated
/// pages always verify; this gates the expensive per-file-leaf verification,
/// which dominates verify cost on large repos.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum VerifyScope {
    /// Verify the aggregate/curated pages; skip per-file-leaf verification.
    #[default]
    Aggregates,
    /// Verify every generated page, including per-file leaves.
    All,
}

impl VerifyScope {
    /// Whether per-file-leaf pages run the grounded verification pass.
    pub fn verifies_leaves(self) -> bool {
        matches!(self, Self::All)
    }
}

#[derive(Clone, Debug, Default)]
pub struct CodewikiAiOptions {
    pub routing: Option<AiRouting>,
    pub depth: AiDepth,
    /// Output verbosity (per-page token budget). Default keeps prior behavior.
    pub prose_depth: ProseDepth,
    /// Audience register layered onto generation prompts. `None` keeps the base
    /// voice; grounding rules hold in every register.
    pub register: Option<ProseRegister>,
    /// Daemon feature profile override for aggregate docs. `None` (the default)
    /// routes aggregate/curated writing through the standard generate path with
    /// the binding's default profile (see `text/generation.rs`); `Some(profile)`
    /// pins that named daemon feature profile instead.
    pub aggregate_profile: Option<String>,
    /// Override seams for the grounded verification pass. Each `None` falls
    /// back to the resolved `ai.text_generate.verify_*` config, then to the
    /// generate model/key and [`super::DEFAULT_VERIFY_PROFILE`]. Kept here so
    /// the generator set is resolved from one options value and the precedence
    /// is unit-testable.
    pub verify_profile: Option<String>,
    pub verify_model: Option<String>,
    pub verify_api_key: Option<String>,
    /// Which pages run the grounded verification pass. Default
    /// ([`VerifyScope::Aggregates`]) verifies the aggregate/curated pages and
    /// skips the expensive per-file-leaf verification; [`VerifyScope::All`]
    /// restores leaf verification.
    pub verify_scope: VerifyScope,
}

impl SourceSpan {
    pub(crate) fn from_symbol(symbol: &Symbol) -> Self {
        Self {
            file: symbol.file_path.clone(),
            line_start: symbol.line_start,
            line_end: symbol.line_end,
        }
    }

    pub(crate) fn citation(&self) -> String {
        if self.line_start == self.line_end {
            format!("[{}:{}]", self.file, self.line_start)
        } else {
            format!("[{}:{}-{}]", self.file, self.line_start, self.line_end)
        }
    }

    pub(crate) fn contains(&self, file: &str, line_start: usize, line_end: usize) -> bool {
        self.file == file && self.line_start <= line_start && line_end <= self.line_end
    }
}
