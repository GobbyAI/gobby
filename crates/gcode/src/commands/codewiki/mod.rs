use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use gobby_core::ai::{
    daemon::generate_via_daemon_with_max_tokens, effective_route, text::generate_text,
};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions, PostgresAiConfigSource};
use gobby_core::config::{AiCapability, AiRouting};
use serde::{Deserialize, Serialize};

use crate::commands::scope;
use crate::config::{self, Context};
use crate::db;
use crate::graph::typed_query;
use crate::index::hasher;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::secrets;
use crate::visibility;

const DEFAULT_OUT_DIR: &str = "codewiki";
const CODEWIKI_META_PATH: &str = "_meta/codewiki.json";
const OWNERSHIP_META_PATH: &str = "_meta/ownership.json";
const MAX_MERMAID_HOPS: usize = 2;
const MAX_MERMAID_EDGES: usize = 20;
const MAX_EDGE_LIMIT: usize = 100_000;

mod build;
mod cluster;
mod graph;
mod io;
mod ownership;
mod paths;
mod progress;
mod prompts;
mod render;
mod reuse;
mod text;

// Document builders.
pub(crate) use build::{
    FileDocPosition, build_architecture_doc, build_codewiki_changes_doc,
    build_codewiki_index_snapshot, build_file_doc, build_hotspots_doc, build_module_docs,
    build_onboarding_doc,
};
// Module clustering and graph-to-file helpers.
pub(crate) use cluster::{
    cluster_file_modules, files_for_import_target, first_component_for_file,
    symbols_by_file_component,
};
#[cfg(test)]
pub(crate) use cluster::{common_module_for_files, find_file_root};
// Optional FalkorDB graph queries.
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
    module_is_ancestor, module_wikilink, parent_module, plural,
};
// Rendered markdown and graph diagrams.
pub(crate) use render::{
    build_repo_doc, render_architecture_dependency_mermaid, render_architecture_doc,
    render_file_doc, render_hotspots_doc, render_module_call_mermaid,
    render_module_dependency_mermaid, render_module_doc, render_onboarding_doc,
};
// AI and structural text helpers.
pub(crate) use text::{
    Generation, citation_list, citation_markers, collect_link_spans, frontmatter_with_degradation,
    ground_text, maybe_generate, replace_citations_with_markers, resolve_text_generator,
    structural_file_summary, structural_module_summary, structural_repo_summary,
    structural_symbol_purpose, write_references, write_section,
};
#[cfg(test)]
pub(crate) use text::{frontmatter, generate_with_bounded_retry};

#[cfg(test)]
pub(crate) use io::write_incremental_doc_set_with_snapshot;
pub(crate) use io::{DocPruneScope, DocSink, read_ownership_meta, write_ownership_meta};
pub use io::{write_doc_set, write_incremental_doc_set};
// Reuse of unchanged docs without regeneration.
pub(crate) use reuse::{ReusePlan, span_files};

#[derive(Debug, Clone)]
pub struct CodewikiInput {
    pub files: Vec<String>,
    pub graph_edges: Vec<CodewikiGraphEdge>,
    pub graph_availability: CodewikiGraphAvailability,
    pub symbols: Vec<Symbol>,
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
    edges: Vec<CodewikiGraphEdge>,
    availability: CodewikiGraphAvailability,
}

impl CodewikiGraph {
    fn available(edges: Vec<CodewikiGraphEdge>) -> Self {
        Self {
            edges,
            availability: CodewikiGraphAvailability::Available,
        }
    }

    fn truncated(edges: Vec<CodewikiGraphEdge>) -> Self {
        Self {
            edges,
            availability: CodewikiGraphAvailability::Truncated,
        }
    }

    fn unavailable() -> Self {
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
    path: String,
    module: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
    symbols: Vec<SymbolDoc>,
    component_ids: Vec<String>,
    /// True when AI generation was attempted for this doc and failed.
    degraded: bool,
    /// The on-disk page when the doc was reused without regeneration (#681);
    /// emitting disk content verbatim keeps a forced rewrite lossless.
    reused_page: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct SymbolDoc {
    symbol: Symbol,
    purpose: String,
    component_id: String,
    component_label: String,
    source_span: SourceSpan,
}

#[derive(Debug, Clone)]
pub(crate) struct ModuleDoc {
    module: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
    direct_files: Vec<FileLink>,
    child_modules: Vec<ModuleLink>,
    component_ids: Vec<String>,
    dependency_diagram: Option<String>,
    call_diagram: Option<String>,
    graph_availability: CodewikiGraphAvailability,
    /// True when AI generation was attempted for this doc and failed.
    degraded: bool,
    /// The on-disk page when the doc was reused without regeneration (#681);
    /// emitting disk content verbatim keeps a forced rewrite lossless.
    reused_page: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ArchitectureDoc {
    source_spans: Vec<SourceSpan>,
    subsystems: Vec<ArchitectureSubsystem>,
    dependency_diagram: Option<String>,
    degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ArchitectureSubsystem {
    module: String,
    responsibility: String,
    source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone)]
pub(crate) struct OnboardingDoc {
    source_spans: Vec<SourceSpan>,
    entry_points: Vec<OnboardingEntryPoint>,
    reading_order: Vec<OnboardingStep>,
    degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct OnboardingEntryPoint {
    link: String,
    description: String,
    source_span: SourceSpan,
}

#[derive(Debug, Clone)]
pub(crate) struct OnboardingStep {
    module: String,
    summary: String,
    degree: usize,
    score: f64,
}

#[derive(Debug, Clone)]
pub(crate) struct HotspotsDoc {
    source_spans: Vec<SourceSpan>,
    hotspots: Vec<HotspotFinding>,
    god_nodes: Vec<HotspotFinding>,
    bridges: Vec<HotspotFinding>,
    degraded_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct HotspotFinding {
    node: HotspotNode,
    degree: Option<usize>,
    score: Option<f64>,
    frequency: Option<usize>,
    weight: Option<f64>,
}

#[derive(Debug, Clone)]
pub(crate) struct HotspotNode {
    id: String,
    kind: String,
    label: String,
    wikilink: String,
    file_wikilink: Option<String>,
    source_span: Option<SourceSpan>,
}

#[derive(Debug, Clone)]
pub(crate) struct FileLink {
    path: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone)]
pub(crate) struct ModuleLink {
    module: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) struct SourceSpan {
    file: String,
    line_start: usize,
    line_end: usize,
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
    docs: BTreeMap<String, CodewikiDocMeta>,
    generated_docs: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    index_snapshot: Option<CodewikiIndexSnapshot>,
    #[serde(default)]
    ai_mode: String,
}

#[derive(Debug, Clone, Default, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiDocMeta {
    source_hashes: BTreeMap<String, String>,
    /// True when the doc on disk was written from a failed generation
    /// fallback. Source hashes cannot see generation failures, so this flag
    /// is what lets a later successful run repair the doc (#687).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    degraded: bool,
    /// The grounded summary this doc feeds into parent prompts and pages,
    /// recorded so an unchanged doc can be reused without an LLM call (#681).
    /// Absent for degraded fallbacks and for docs nothing consumes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    summary: Option<String>,
    /// AI mode the doc on disk was generated under. Entries written before
    /// per-doc modes existed inherit the run-level `ai_mode` at read time.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    ai_mode: String,
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
    files: BTreeMap<String, CodewikiFileSnapshot>,
    symbols: BTreeMap<String, CodewikiSymbolSnapshot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    graph_neighborhoods: Option<BTreeMap<String, String>>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    degraded_sources: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiFileSnapshot {
    content_hash: String,
    symbol_count: usize,
}

#[derive(Debug, Clone, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiSymbolSnapshot {
    file_path: String,
    name: String,
    qualified_name: String,
    kind: String,
    line_start: usize,
}

pub type TextGenerator<'a> = dyn FnMut(&str, &str, PromptTier) -> Option<String> + 'a;

/// Default daemon feature profile for aggregate (module/repo/architecture)
/// prose, which synthesizes 10k+-token grounded prompts; file and symbol
/// docs stay on the daemon's default low-tier profile.
pub(crate) const DEFAULT_AGGREGATE_PROFILE: &str = "feature_mid";

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

    fn mode_label(self) -> &'static str {
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
    /// [`DEFAULT_AGGREGATE_PROFILE`].
    pub aggregate_profile: Option<String>,
}

impl SourceSpan {
    fn from_symbol(symbol: &Symbol) -> Self {
        Self {
            file: symbol.file_path.clone(),
            line_start: symbol.line_start,
            line_end: symbol.line_end,
        }
    }

    fn citation(&self) -> String {
        if self.line_start == self.line_end {
            format!("[{}:{}]", self.file, self.line_start)
        } else {
            format!("[{}:{}-{}]", self.file, self.line_start, self.line_end)
        }
    }

    fn contains(&self, file: &str, line_start: usize, line_end: usize) -> bool {
        self.file == file && self.line_start <= line_start && line_end <= self.line_end
    }
}

pub fn run(
    ctx: &Context,
    out: Option<String>,
    scope_args: Vec<String>,
    ai: CodewikiAiOptions,
    edge_limit: usize,
    format: Format,
    verbose: bool,
) -> anyhow::Result<()> {
    validate_edge_limit(edge_limit)?;
    let ai_depth = ai.depth;

    let mut progress = CodewikiProgress::stderr(verbose && !ctx.quiet);

    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let scopes = scope_args
        .iter()
        .map(|value| scope::normalize_file_arg(ctx, value))
        .collect::<Vec<_>>();
    progress.emit("loading indexed files");
    let files = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .map(|file| file.file_path)
        .filter(|file| in_scope(file, &scopes))
        .collect::<Vec<_>>();
    let symbols = load_symbols_for_codewiki(&files, &mut progress, |paths| {
        visibility::visible_symbols_for_files(&mut conn, ctx, paths)
    })?;

    progress.emit(format!(
        "fetching graph edges for {} files and {} symbols (limit {})",
        files.len(),
        symbols.len(),
        edge_limit
    ));
    let graph = fetch_codewiki_graph_edges(ctx, &files, &symbols, edge_limit)?;
    let input = CodewikiInput {
        files,
        graph_edges: graph.edges,
        graph_availability: graph.availability,
        symbols,
    };
    let mut generator = resolve_text_generator(ctx, &ai);
    let ai_enabled = generator.is_some();
    let ai_mode = if ai_enabled {
        ai_depth.mode_label()
    } else {
        "off"
    };
    let out_dir = out.unwrap_or_else(|| DEFAULT_OUT_DIR.to_string());
    let out_path = Path::new(&out_dir);
    progress.emit("reading metadata and hashing snapshot");
    let previous_meta = io::read_codewiki_meta(out_path)?;
    let index_snapshot = build_codewiki_index_snapshot(&ctx.project_root, &input)?;
    let mut ownership_meta = read_ownership_meta(out_path)?;
    let mut reuse_plan = ReusePlan::load(&ctx.project_root, out_path, ai_mode)?;
    let mut reuse = Some(&mut reuse_plan);
    let mut sink = DocSink::open_with_prune_scope(
        &ctx.project_root,
        out_path,
        ai_mode,
        DocPruneScope::from_scopes(&scopes),
    )?;
    let mut generated_pages = 0_usize;
    let mut module_count = 0_usize;
    let mut file_count = 0_usize;
    // Persist each doc and its meta entry as soon as it is built, so a killed
    // run keeps everything generated so far and a re-run resumes from disk.
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        generated_pages += 1;
        if doc.path.starts_with("code/modules/") {
            module_count += 1;
        }
        if doc.path.starts_with("code/files/") {
            file_count += 1;
        }
        sink.persist(&doc)?;
        Ok(())
    };
    generate_hierarchical_docs_with_ownership(
        &input,
        &ctx.project_root,
        &mut ownership_meta,
        generator.as_deref_mut(),
        ai_depth,
        &mut reuse,
        &mut progress,
        &mut emit,
    )?;
    progress.emit("generating changes docs");
    emit(BuiltDoc::healthy(
        "code/_changes.md",
        build_codewiki_changes_doc(previous_meta.index_snapshot.as_ref(), &index_snapshot)?,
    ))?;
    write_ownership_meta(out_path, &ownership_meta)?;
    let symbol_count = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .count();
    let changed_paths = sink.finish(Some(index_snapshot))?;
    let skipped = generated_pages.saturating_sub(changed_paths.len());

    let summary = CodewikiRunSummary {
        command: "codewiki",
        project_id: ctx.project_id.clone(),
        project_root: ctx.project_root.display().to_string(),
        out_dir,
        generated_pages,
        changed_paths,
        skipped,
        files: file_count,
        modules: module_count,
        symbols: symbol_count,
        ai_enabled,
    };
    match format {
        Format::Json => output::print_json(&summary),
        Format::Text => output::print_text(&format!(
            "wrote {} file docs, {} module docs, and repo.md to {}",
            summary.files, summary.modules, summary.out_dir
        )),
    }?;

    Ok(())
}

fn validate_edge_limit(edge_limit: usize) -> anyhow::Result<()> {
    if (1..=MAX_EDGE_LIMIT).contains(&edge_limit) {
        return Ok(());
    }
    anyhow::bail!("codewiki --edge-limit must be between 1 and {MAX_EDGE_LIMIT}, got {edge_limit}")
}

fn load_symbols_for_codewiki(
    files: &[String],
    progress: &mut CodewikiProgress,
    mut load_symbols: impl FnMut(&[String]) -> anyhow::Result<Vec<Symbol>>,
) -> anyhow::Result<Vec<Symbol>> {
    progress.emit(format!("loading symbols for {} files", files.len()));
    load_symbols(files)
}

pub fn generate_hierarchical_docs(
    input: &CodewikiInput,
    generate: Option<&mut TextGenerator<'_>>,
) -> Vec<(String, String)> {
    generate_hierarchical_docs_with_graph_availability(input, generate)
        .into_iter()
        .map(|doc| (doc.path, doc.content))
        .collect()
}

fn generate_hierarchical_docs_with_graph_availability(
    input: &CodewikiInput,
    mut generate: Option<&mut TextGenerator<'_>>,
) -> Vec<BuiltDoc> {
    let mut progress = CodewikiProgress::silent();
    let mut docs = Vec::new();
    if let Err(error) = generate_hierarchical_docs_core(
        input,
        None,
        &mut generate,
        AiDepth::Symbols,
        &mut None,
        &mut progress,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    ) {
        log::warn!("codewiki generation failed without ownership metadata: {error}");
        return Vec::new();
    }
    docs
}

#[expect(clippy::too_many_arguments)]
fn generate_hierarchical_docs_with_ownership(
    input: &CodewikiInput,
    project_root: &Path,
    ownership_meta: &mut OwnershipMeta,
    mut generate: Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    generate_hierarchical_docs_core(
        input,
        Some((project_root, ownership_meta)),
        &mut generate,
        ai_depth,
        reuse,
        progress,
        emit,
    )
}

#[cfg(test)]
fn generate_hierarchical_docs_with_progress(
    input: &CodewikiInput,
    generate: Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
) -> Vec<BuiltDoc> {
    generate_hierarchical_docs_with_reuse(input, generate, ai_depth, &mut None, progress)
}

/// Test entry point that exercises the reuse path without the CLI runtime.
#[cfg(test)]
fn generate_hierarchical_docs_with_reuse(
    input: &CodewikiInput,
    mut generate: Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> Vec<BuiltDoc> {
    let mut docs = Vec::new();
    if let Err(error) = generate_hierarchical_docs_core(
        input,
        None,
        &mut generate,
        ai_depth,
        reuse,
        progress,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    ) {
        log::warn!("codewiki generation failed without ownership metadata: {error}");
        return Vec::new();
    }
    docs
}

fn generate_hierarchical_docs_core(
    input: &CodewikiInput,
    ownership: Option<(&Path, &mut OwnershipMeta)>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    ai_depth: AiDepth,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    let mut files = input
        .files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<BTreeSet<_>>();
    for symbol in &input.symbols {
        if is_core_file(&symbol.file_path) {
            files.insert(symbol.file_path.clone());
        }
    }
    let files = files.into_iter().collect::<Vec<_>>();

    let mut symbols_by_file: BTreeMap<String, Vec<Symbol>> = BTreeMap::new();
    for symbol in &input.symbols {
        if !is_core_file(&symbol.file_path) {
            continue;
        }
        symbols_by_file
            .entry(symbol.file_path.clone())
            .or_default()
            .push(symbol.clone());
    }
    for symbols in symbols_by_file.values_mut() {
        symbols.sort_by_key(|symbol| (symbol.line_start, symbol.byte_start, symbol.name.clone()));
    }

    let file_modules = cluster_file_modules(&files, &symbols_by_file, &input.graph_edges);
    let file_verb = if ai_depth.includes_files() {
        "generating"
    } else {
        "building"
    };
    progress.emit(format!("{file_verb} file docs for {} files", files.len()));
    let file_total = files.len();
    let mut file_docs = Vec::with_capacity(file_total);
    for (index, file) in files.iter().enumerate() {
        let file_doc = build_file_doc(
            file,
            file_modules
                .get(file)
                .cloned()
                .unwrap_or_else(|| module_for_file(file)),
            symbols_by_file.remove(file).unwrap_or_default(),
            generate,
            reuse,
            ai_depth,
            progress,
            FileDocPosition {
                index: index + 1,
                total: file_total,
            },
        );
        emit(BuiltDoc {
            path: file_doc_path(&file_doc.path),
            content: file_doc
                .reused_page
                .clone()
                .unwrap_or_else(|| render_file_doc(&file_doc)),
            degraded: file_doc.degraded,
            summary: Some(file_doc.summary.clone()),
        })?;
        file_docs.push(file_doc);
    }
    progress.emit("generating module docs");
    let module_docs = build_module_docs(
        &file_docs,
        &input.graph_edges,
        input.graph_availability,
        generate,
        reuse,
        progress,
        &mut |module| {
            emit(BuiltDoc {
                path: module_doc_path(&module.module),
                content: module
                    .reused_page
                    .clone()
                    .unwrap_or_else(|| render_module_doc(module)),
                degraded: module.degraded,
                summary: Some(module.summary.clone()),
            })
        },
    )?;
    let (repo_doc, repo_degraded) =
        build_repo_doc(&file_docs, &module_docs, generate, reuse, progress);
    emit(BuiltDoc {
        path: "code/repo.md".to_string(),
        content: repo_doc,
        degraded: repo_degraded,
        summary: None,
    })?;
    progress.emit("generating architecture docs");
    // Architecture provenance covers every subsystem source, so the page is
    // reusable only when nothing changed at all — then the per-subsystem
    // generation loop is skipped entirely and the on-disk page kept.
    let subsystem_names = file_docs
        .iter()
        .map(|file| file.module.clone())
        .collect::<BTreeSet<_>>();
    let architecture_sources = span_files(
        &module_docs
            .iter()
            .filter(|module| subsystem_names.contains(&module.module))
            .flat_map(|module| module.source_spans.iter().cloned())
            .collect::<Vec<_>>(),
    );
    let reused_architecture = reuse
        .as_deref_mut()
        .and_then(|plan| plan.reusable_page("code/_architecture.md", &architecture_sources));
    let architecture_built = match reused_architecture {
        Some(page) => {
            progress.emit("reusing architecture docs (sources unchanged)");
            BuiltDoc::healthy("code/_architecture.md", page)
        }
        None => {
            let architecture_doc = build_architecture_doc(
                &file_docs,
                &module_docs,
                &input.graph_edges,
                input.graph_availability,
                generate,
                progress,
            );
            BuiltDoc {
                path: "code/_architecture.md".to_string(),
                content: render_architecture_doc(&architecture_doc),
                degraded: architecture_doc
                    .degraded_sources
                    .iter()
                    .any(|source| source == "model-unavailable"),
                summary: None,
            }
        }
    };
    emit(architecture_built)?;
    progress.emit("generating onboarding docs");
    let onboarding_doc = build_onboarding_doc(
        &file_docs,
        &module_docs,
        &input.graph_edges,
        input.graph_availability,
    );
    emit(BuiltDoc::healthy(
        "code/_onboarding.md",
        render_onboarding_doc(&onboarding_doc),
    ))?;
    progress.emit("generating hotspots docs");
    let hotspots_doc = build_hotspots_doc(&file_docs, &input.graph_edges, input.graph_availability);
    emit(BuiltDoc::healthy(
        "code/_hotspots.md",
        render_hotspots_doc(&hotspots_doc),
    ))?;
    if let Some((project_root, ownership_meta)) = ownership {
        progress.emit("generating ownership docs");
        emit(BuiltDoc::healthy(
            "code/_ownership.md",
            build_ownership_doc(
                project_root,
                &files,
                &file_modules,
                ownership_meta,
                OwnershipOptions::default(),
            )?,
        ))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
