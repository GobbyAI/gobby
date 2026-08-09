use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use gobby_code::codewiki_facts::SymbolFact;

use super::GenerationObservability;
use super::diagram_compose::DiagramStats;
use super::prompts;

mod ai;

pub(crate) use ai::{
    ARCHITECTURE_DOC_PATH, AiGenerationSettings, AiGenerationStatus, CodewikiAiOutcome,
    REPO_DOC_PATH, SyncTextGenerator, SyncTextVerifier, ai_outcome_for_doc, is_curated_doc,
};
pub use ai::{
    AiDepth, CodewikiAiOptions, PromptTier, ProseDepth, ProseRegister, TextGenerator, TextVerifier,
    VerifyScope,
};

/// Wiki-owned projection of an indexed symbol.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Symbol {
    pub(crate) id: String,
    pub(crate) project_id: String,
    pub(crate) file_path: String,
    pub(crate) name: String,
    pub(crate) qualified_name: String,
    pub(crate) kind: String,
    pub(crate) language: String,
    pub(crate) byte_start: usize,
    pub(crate) byte_end: usize,
    pub(crate) line_start: usize,
    pub(crate) line_end: usize,
    pub(crate) signature: Option<String>,
    pub(crate) docstring: Option<String>,
    pub(crate) parent_symbol_id: Option<String>,
    pub(crate) file_content_hash: String,
    pub(crate) content_hash: String,
    pub(crate) summary: Option<String>,
}

impl Symbol {
    #[cfg(test)]
    pub(crate) fn make_id(
        project_id: &str,
        file_path: &str,
        file_content_hash: &str,
        name: &str,
        kind: &str,
        byte_start: usize,
    ) -> String {
        const CODE_INDEX_NAMESPACE: uuid::Uuid =
            uuid::Uuid::from_u128(0xc0de1de0000040008000000000000000);
        let key =
            format!("{project_id}:{file_path}:{file_content_hash}:{name}:{kind}:{byte_start}");
        uuid::Uuid::new_v5(&CODE_INDEX_NAMESPACE, key.as_bytes()).to_string()
    }

    pub(crate) fn from_fact(fact: SymbolFact, project_id: &str) -> Self {
        Self {
            id: fact.id,
            project_id: project_id.to_owned(),
            file_path: fact.file_path,
            name: fact.name,
            qualified_name: fact.qualified_name,
            kind: fact.kind,
            language: fact.language,
            byte_start: fact.byte_start,
            byte_end: fact.byte_end,
            line_start: fact.line_start,
            line_end: fact.line_end,
            signature: fact.signature,
            docstring: fact.docstring,
            parent_symbol_id: fact.parent_symbol_id,
            file_content_hash: fact.file_content_hash,
            content_hash: fact.content_hash,
            summary: fact.summary,
        }
    }
}

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

#[derive(Debug, Clone, Eq, PartialEq)]
pub(crate) struct CommitStamp {
    pub(crate) sha: String,
    pub(crate) dirty: bool,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) commit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) commit_dirty: Option<bool>,
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
    pub(crate) dependency_diagram: Option<String>,
    pub(crate) call_sequence_diagram: Option<String>,
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
    /// Accumulated tool-loop observability across the subsystem and
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
    /// Frontmatter provenance spans: each section's curated adapter file,
    /// kept only when that file is part of the documented input set so the
    /// reuse machinery can hash it (#17781).
    pub(crate) source_spans: Vec<SourceSpan>,
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
    /// Repo-relative path of the pinned contract JSON this section was
    /// projected from, stamped as page provenance (#17781).
    pub(crate) contract_file: String,
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
    pub(crate) commit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) commit_dirty: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) index_snapshot: Option<CodewikiIndexSnapshot>,
    #[serde(default)]
    pub(crate) ai_mode: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) diagram_stats: Option<DiagramStats>,
}

#[derive(Debug, Clone, Default, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiDocMeta {
    pub(crate) source_hashes: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) commit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) commit_dirty: Option<bool>,
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
    /// Tool-loop observability mirrored from the page frontmatter for an
    /// aggregate page produced by the tool loop (#978): the generation lane and
    /// the loop's call/turn counts. `None` for one-shot / leaf / deterministic
    /// pages. Recorded for traceability; not part of the reuse-invalidation
    /// comparison.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) lane: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) tool_call_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) turns: Option<usize>,
    /// Requested AI generation settings the prose on disk was written under
    /// (#17530). Content hashes cannot see a settings change — the same sources
    /// prompt a different writer — so these are part of the reuse comparison:
    /// a prose-depth or register change re-voices every AI page, while an
    /// aggregate profile/candidate change re-voices only the aggregate-writer
    /// pages (see [`AiGenerationSettings::for_path`]). Every field records the
    /// *requested* value and stays empty for the default, so a default run's
    /// meta is byte-identical to one written before these fields existed.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) ai_prose_depth: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) ai_register: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) ai_aggregate_profile: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) ai_aggregate_candidates: Vec<String>,
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
            self.content = super::strict_markdown::normalize_codewiki_markdown(&self.content);
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
