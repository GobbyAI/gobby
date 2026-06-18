use std::collections::BTreeMap;

use gobby_core::config::AiRouting;
use serde::{Deserialize, Serialize};

use crate::models::Symbol;

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
}

#[derive(Debug, Clone)]
pub(crate) struct ModuleDoc {
    pub(crate) module: String,
    pub(crate) summary: String,
    pub(crate) source_spans: Vec<SourceSpan>,
    pub(crate) direct_files: Vec<FileLink>,
    pub(crate) child_modules: Vec<ModuleLink>,
    pub(crate) dependency_diagram: Option<String>,
    pub(crate) call_diagram: Option<String>,
    pub(crate) graph_availability: CodewikiGraphAvailability,
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
    pub(crate) dependency_diagram: Option<String>,
    pub(crate) degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ArchitectureSubsystem {
    pub(crate) module: String,
    pub(crate) responsibility: String,
    pub(crate) child_modules: Vec<String>,
    pub(crate) source_spans: Vec<SourceSpan>,
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
    /// Render-template version for deterministic markdown emitted after model
    /// generation. Missing versions force a one-time rewrite on upgrade.
    #[serde(default)]
    pub(crate) render_version: u32,
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
}

impl BuiltDoc {
    pub(crate) fn healthy(path: impl Into<String>, content: String) -> Self {
        Self {
            path: path.into(),
            content,
            degraded: false,
            summary: None,
        }
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

/// Weight tier of one codewiki generation call. Aggregate docs roll up many
/// child summaries into one long grounded synthesis and route to a heavier
/// daemon profile; standard calls (file summaries, symbol purposes) are
/// high-volume and stay on the default profile.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum PromptTier {
    #[default]
    Standard,
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

#[derive(Clone, Debug, Default)]
pub struct CodewikiAiOptions {
    pub routing: Option<AiRouting>,
    pub depth: AiDepth,
    /// Daemon feature profile for aggregate docs; `None` means
    /// [`super::DEFAULT_AGGREGATE_PROFILE`].
    pub aggregate_profile: Option<String>,
    /// Override seams for the grounded verification pass. There is no CLI flag
    /// for these (verify is config-only): each `None` falls back to the resolved
    /// `ai.text_generate.verify_*` config, then to the generate model/key and
    /// [`super::DEFAULT_VERIFY_PROFILE`]. Kept here so the generator set is
    /// resolved from one options value and the precedence is unit-testable.
    pub verify_profile: Option<String>,
    pub verify_model: Option<String>,
    pub verify_api_key: Option<String>,
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
