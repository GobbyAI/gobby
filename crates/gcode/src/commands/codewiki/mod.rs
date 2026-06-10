use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use gobby_core::ai::{daemon::generate_via_daemon, effective_route, text::generate_text};
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
mod text;

// Document builders.
pub(crate) use build::{
    build_architecture_doc, build_codewiki_changes_doc, build_codewiki_index_snapshot,
    build_file_doc, build_hotspots_doc, build_module_docs, build_onboarding_doc,
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
pub(crate) use graph::{codewiki_call_edges_query, codewiki_import_edges_query};
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
    citation_list, citation_markers, collect_link_spans, frontmatter, frontmatter_with_degradation,
    ground_text, maybe_generate, replace_citations_with_markers, resolve_text_generator,
    structural_file_summary, structural_module_summary, structural_repo_summary,
    structural_symbol_purpose, write_references, write_section,
};

pub(crate) use io::{
    read_ownership_meta, write_incremental_doc_set_with_snapshot, write_ownership_meta,
};
pub use io::{write_doc_set, write_incremental_doc_set};

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
}

#[derive(Debug, Clone, Default, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CodewikiDocMeta {
    source_hashes: BTreeMap<String, String>,
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

pub type TextGenerator<'a> = dyn FnMut(&str, &str) -> Option<String> + 'a;

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
    ai: Option<AiRouting>,
    edge_limit: usize,
    format: Format,
    verbose: bool,
) -> anyhow::Result<()> {
    validate_edge_limit(edge_limit)?;

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
    progress.emit(format!("loading symbols for {} files", files.len()));
    let mut symbols = Vec::new();
    for (index, file) in files.iter().enumerate() {
        progress.emit(format!(
            "loading symbols file {}/{} {}",
            index + 1,
            files.len(),
            file
        ));
        symbols.extend(visibility::visible_symbols_for_file(&mut conn, ctx, file)?);
    }

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
    let mut generator = resolve_text_generator(ctx, ai);
    let ai_enabled = generator.is_some();
    let out_dir = out.unwrap_or_else(|| DEFAULT_OUT_DIR.to_string());
    let out_path = Path::new(&out_dir);
    progress.emit("reading metadata and hashing snapshot");
    let previous_meta = io::read_codewiki_meta(out_path)?;
    let index_snapshot = build_codewiki_index_snapshot(&ctx.project_root, &input)?;
    let mut ownership_meta = read_ownership_meta(out_path)?;
    let mut docs = generate_hierarchical_docs_with_ownership(
        &input,
        &ctx.project_root,
        &mut ownership_meta,
        generator.as_deref_mut(),
        &mut progress,
    )?;
    progress.emit("generating changes docs");
    docs.push((
        "code/_changes.md".to_string(),
        build_codewiki_changes_doc(previous_meta.index_snapshot.as_ref(), &index_snapshot)?,
    ));
    let module_count = docs
        .iter()
        .filter(|(path, _)| path.starts_with("code/modules/"))
        .count();
    let file_count = docs
        .iter()
        .filter(|(path, _)| path.starts_with("code/files/"))
        .count();
    let symbol_count = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .count();
    let changed_paths = write_codewiki_docs(
        &ctx.project_root,
        out_path,
        &docs,
        Some(index_snapshot),
        &ownership_meta,
        &mut progress,
    )?;
    let generated_pages = docs.len();
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

fn write_codewiki_docs(
    project_root: &Path,
    out_path: &Path,
    docs: &[(String, String)],
    index_snapshot: Option<CodewikiIndexSnapshot>,
    ownership_meta: &OwnershipMeta,
    progress: &mut CodewikiProgress,
) -> anyhow::Result<Vec<String>> {
    progress.emit(format!(
        "writing docs {} pages to {}",
        docs.len(),
        out_path.display()
    ));
    let changed_paths =
        write_incremental_doc_set_with_snapshot(project_root, out_path, docs, index_snapshot)?;
    write_ownership_meta(out_path, ownership_meta)?;
    Ok(changed_paths)
}

pub fn generate_hierarchical_docs(
    input: &CodewikiInput,
    generate: Option<&mut TextGenerator<'_>>,
) -> Vec<(String, String)> {
    generate_hierarchical_docs_with_graph_availability(input, generate)
}

fn generate_hierarchical_docs_with_graph_availability(
    input: &CodewikiInput,
    mut generate: Option<&mut TextGenerator<'_>>,
) -> Vec<(String, String)> {
    let mut progress = CodewikiProgress::silent();
    match generate_hierarchical_docs_core(input, None, &mut generate, &mut progress) {
        Ok(docs) => docs,
        Err(error) => {
            log::warn!("codewiki generation failed without ownership metadata: {error}");
            Vec::new()
        }
    }
}

fn generate_hierarchical_docs_with_ownership(
    input: &CodewikiInput,
    project_root: &Path,
    ownership_meta: &mut OwnershipMeta,
    mut generate: Option<&mut TextGenerator<'_>>,
    progress: &mut CodewikiProgress,
) -> anyhow::Result<Vec<(String, String)>> {
    generate_hierarchical_docs_core(
        input,
        Some((project_root, ownership_meta)),
        &mut generate,
        progress,
    )
}

#[cfg(test)]
fn generate_hierarchical_docs_with_progress(
    input: &CodewikiInput,
    mut generate: Option<&mut TextGenerator<'_>>,
    progress: &mut CodewikiProgress,
) -> Vec<(String, String)> {
    match generate_hierarchical_docs_core(input, None, &mut generate, progress) {
        Ok(docs) => docs,
        Err(error) => {
            log::warn!("codewiki generation failed without ownership metadata: {error}");
            Vec::new()
        }
    }
}

fn generate_hierarchical_docs_core(
    input: &CodewikiInput,
    ownership: Option<(&Path, &mut OwnershipMeta)>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    progress: &mut CodewikiProgress,
) -> anyhow::Result<Vec<(String, String)>> {
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
    progress.emit(format!("generating file docs for {} files", files.len()));
    let file_total = files.len();
    let file_docs = files
        .iter()
        .enumerate()
        .map(|(index, file)| {
            build_file_doc(
                file,
                file_modules
                    .get(file)
                    .cloned()
                    .unwrap_or_else(|| module_for_file(file)),
                symbols_by_file.remove(file).unwrap_or_default(),
                generate,
                progress,
                index + 1,
                file_total,
            )
        })
        .collect::<Vec<_>>();
    progress.emit("generating module docs");
    let module_docs = build_module_docs(
        &file_docs,
        &input.graph_edges,
        input.graph_availability,
        generate,
        progress,
    );
    let repo_doc = build_repo_doc(&file_docs, &module_docs, generate, progress);
    progress.emit("generating architecture docs");
    let architecture_doc = build_architecture_doc(
        &file_docs,
        &module_docs,
        &input.graph_edges,
        input.graph_availability,
        generate,
        progress,
    );
    progress.emit("generating onboarding docs");
    let onboarding_doc = build_onboarding_doc(
        &file_docs,
        &module_docs,
        &input.graph_edges,
        input.graph_availability,
    );
    progress.emit("generating hotspots docs");
    let hotspots_doc = build_hotspots_doc(&file_docs, &input.graph_edges, input.graph_availability);

    let mut docs = vec![
        ("code/repo.md".to_string(), repo_doc),
        (
            "code/_onboarding.md".to_string(),
            render_onboarding_doc(&onboarding_doc),
        ),
        (
            "code/_architecture.md".to_string(),
            render_architecture_doc(&architecture_doc),
        ),
        (
            "code/_hotspots.md".to_string(),
            render_hotspots_doc(&hotspots_doc),
        ),
    ];
    if let Some((project_root, ownership_meta)) = ownership {
        progress.emit("generating ownership docs");
        docs.push((
            "code/_ownership.md".to_string(),
            build_ownership_doc(
                project_root,
                &files,
                &file_modules,
                ownership_meta,
                OwnershipOptions::default(),
            )?,
        ));
    }
    for module in &module_docs {
        docs.push((module_doc_path(&module.module), render_module_doc(module)));
    }
    for file in &file_docs {
        docs.push((file_doc_path(&file.path), render_file_doc(file)));
    }
    Ok(docs)
}

#[cfg(test)]
mod hotspots_tests;
#[cfg(test)]
mod module_tests;
#[cfg(test)]
mod onboarding_tests;
#[cfg(test)]
mod progress_tests;
#[cfg(test)]
mod test_utils;
#[cfg(test)]
mod tests;
