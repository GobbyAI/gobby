use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use gobby_core::ai::{daemon::generate_via_daemon, effective_route, text::generate_text};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions, PostgresAiConfigSource};
use gobby_core::config::{AiCapability, AiRouting};
use serde::{Deserialize, Serialize};

use crate::commands::scope;
use crate::config::{self, Context};
use crate::db;
use crate::falkor;
use crate::index::hasher;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::secrets;
use crate::visibility;

const DEFAULT_OUT_DIR: &str = "codewiki";
const CODEWIKI_META_PATH: &str = "_meta/codewiki.json";
const MAX_MERMAID_HOPS: usize = 2;
const MAX_MERMAID_EDGES: usize = 20;

mod prompts {
    use std::fmt::Write as _;

    use crate::models::Symbol;

    pub const SYMBOL_SYSTEM: &str = "You write concise API reference notes. Return one sentence describing the symbol's purpose. Do not include markdown fences.";
    pub const FILE_SYSTEM: &str = "You write concise file-level code documentation. Return a short purpose summary that reuses the supplied symbol summaries. Do not include markdown fences.";
    pub const MODULE_SYSTEM: &str = "You write concise module overviews for code documentation. Return a short overview from the supplied child summaries. Do not include markdown fences.";
    pub const REPO_SYSTEM: &str = "You write concise repository overviews for code documentation. Return a short overview from the supplied module summaries. Do not include markdown fences.";

    pub fn symbol_prompt(symbol: &Symbol) -> String {
        let mut prompt = format!(
            "File: {}\nSymbol: {} [{}]\nLines: {}-{}",
            symbol.file_path,
            symbol.qualified_name,
            symbol.kind,
            symbol.line_start,
            symbol.line_end
        );
        if let Some(signature) = symbol
            .signature
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            let _ = write!(prompt, "\nSignature: {signature}");
        }
        if let Some(docstring) = symbol
            .docstring
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            let _ = write!(prompt, "\nExisting docs: {docstring}");
        }
        prompt
    }

    pub fn file_prompt(file: &str, symbols: &[SymbolSummary]) -> String {
        let mut prompt =
            format!("Summarize this file once from its AST symbols.\n\nFile: {file}\n\nSymbols:\n");
        if symbols.is_empty() {
            prompt.push_str("- No indexed symbols.\n");
        } else {
            for symbol in symbols {
                let _ = writeln!(
                    prompt,
                    "- {} [{}] component {} ({}) lines {}-{}: {}",
                    symbol.name,
                    symbol.kind,
                    symbol.component_label,
                    symbol.component_id,
                    symbol.line_start,
                    symbol.line_end,
                    symbol.purpose
                );
            }
        }
        prompt
    }

    pub fn module_prompt(
        module: &str,
        files: &[ChildSummary],
        modules: &[ChildSummary],
        components: &[String],
    ) -> String {
        let mut prompt = format!(
            "Summarize this module once from lower-level summaries.\n\nModule: {module}\n\nFiles:\n"
        );
        if files.is_empty() {
            prompt.push_str("- No direct files.\n");
        } else {
            for file in files {
                let _ = writeln!(prompt, "- {}: {}", file.name, file.summary);
            }
        }
        prompt.push_str("\nChild modules:\n");
        if modules.is_empty() {
            prompt.push_str("- No child modules.\n");
        } else {
            for module in modules {
                let _ = writeln!(prompt, "- {}: {}", module.name, module.summary);
            }
        }
        prompt.push_str("\nStable component IDs:\n");
        if components.is_empty() {
            prompt.push_str("- No indexed components.\n");
        } else {
            for component in components {
                let _ = writeln!(prompt, "- {component}");
            }
        }
        prompt
    }

    pub fn repo_prompt(modules: &[ChildSummary], files: &[ChildSummary]) -> String {
        let mut prompt =
            "Summarize this repository once from module and root-file summaries.\n\nModules:\n"
                .to_string();
        if modules.is_empty() {
            prompt.push_str("- No modules.\n");
        } else {
            for module in modules {
                let _ = writeln!(prompt, "- {}: {}", module.name, module.summary);
            }
        }
        prompt.push_str("\nRoot files:\n");
        if files.is_empty() {
            prompt.push_str("- No root files.\n");
        } else {
            for file in files {
                let _ = writeln!(prompt, "- {}: {}", file.name, file.summary);
            }
        }
        prompt
    }

    #[derive(Debug, Clone)]
    pub struct SymbolSummary {
        pub name: String,
        pub kind: String,
        pub component_id: String,
        pub component_label: String,
        pub line_start: usize,
        pub line_end: usize,
        pub purpose: String,
    }

    #[derive(Debug, Clone)]
    pub struct ChildSummary {
        pub name: String,
        pub summary: String,
    }
}

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
struct CodewikiGraph {
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
    Unavailable,
}

#[derive(Debug, Clone)]
struct FileDoc {
    path: String,
    module: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
    symbols: Vec<SymbolDoc>,
    component_ids: Vec<String>,
}

#[derive(Debug, Clone)]
struct SymbolDoc {
    symbol: Symbol,
    purpose: String,
    component_id: String,
    component_label: String,
    source_span: SourceSpan,
}

#[derive(Debug, Clone)]
struct ModuleDoc {
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
struct FileLink {
    path: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone)]
struct ModuleLink {
    module: String,
    summary: String,
    source_spans: Vec<SourceSpan>,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
struct SourceSpan {
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
struct CodewikiMeta {
    docs: BTreeMap<String, CodewikiDocMeta>,
    generated_docs: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Eq, PartialEq, Serialize)]
struct CodewikiDocMeta {
    source_hashes: BTreeMap<String, String>,
}

pub type TextGenerator<'a> = dyn FnMut(&str, &str) -> Option<String> + 'a;

pub fn run(
    ctx: &Context,
    out: Option<String>,
    scope_args: Vec<String>,
    ai: Option<AiRouting>,
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let scopes = scope_args
        .iter()
        .map(|value| scope::normalize_file_arg(ctx, value))
        .collect::<Vec<_>>();
    let files = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .map(|file| file.file_path)
        .filter(|file| in_scope(file, &scopes))
        .collect::<Vec<_>>();
    let mut symbols = Vec::new();
    for file in &files {
        symbols.extend(visibility::visible_symbols_for_file(&mut conn, ctx, file)?);
    }

    let graph = fetch_codewiki_graph_edges(ctx, &files, &symbols)?;
    let input = CodewikiInput {
        files,
        graph_edges: graph.edges,
        graph_availability: graph.availability,
        symbols,
    };
    let mut generator = resolve_text_generator(ctx, ai);
    let ai_enabled = generator.is_some();
    let docs = match generator.as_deref_mut() {
        Some(generate) => generate_hierarchical_docs(&input, Some(generate)),
        None => generate_hierarchical_docs(&input, None),
    };
    let module_count = docs
        .iter()
        .filter(|(path, _)| path.starts_with("modules/"))
        .count();
    let file_count = docs
        .iter()
        .filter(|(path, _)| path.starts_with("files/"))
        .count();
    let symbol_count = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .count();
    let out_dir = out.unwrap_or_else(|| DEFAULT_OUT_DIR.to_string());
    let changed_paths = write_incremental_doc_set(&ctx.project_root, Path::new(&out_dir), &docs)?;
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
    }
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
    let file_docs = files
        .iter()
        .map(|file| {
            build_file_doc(
                file,
                file_modules
                    .get(file)
                    .cloned()
                    .unwrap_or_else(|| module_for_file(file)),
                symbols_by_file.remove(file).unwrap_or_default(),
                &mut generate,
            )
        })
        .collect::<Vec<_>>();
    let module_docs = build_module_docs(
        &file_docs,
        &input.graph_edges,
        input.graph_availability,
        &mut generate,
    );
    let repo_doc = build_repo_doc(&file_docs, &module_docs, &mut generate);

    let mut docs = Vec::new();
    docs.push(("repo.md".to_string(), repo_doc));
    for module in &module_docs {
        docs.push((module_doc_path(&module.module), render_module_doc(module)));
    }
    for file in &file_docs {
        docs.push((file_doc_path(&file.path), render_file_doc(file)));
    }
    docs
}

pub fn write_doc_set(out_dir: &Path, docs: &[(String, String)]) -> anyhow::Result<()> {
    std::fs::create_dir_all(out_dir)?;
    for (relative_path, content) in docs {
        write_doc(out_dir, relative_path, content)?;
    }
    Ok(())
}

pub fn write_incremental_doc_set(
    project_root: &Path,
    out_dir: &Path,
    docs: &[(String, String)],
) -> anyhow::Result<Vec<String>> {
    std::fs::create_dir_all(out_dir)?;
    let previous = read_codewiki_meta(out_dir)?;
    let mut next_docs = BTreeMap::new();
    let mut generated_docs = Vec::new();

    for (relative_path, content) in docs {
        let doc_meta = CodewikiDocMeta {
            source_hashes: source_hashes_for_doc(project_root, content)?,
        };
        let target = safe_doc_path(out_dir, relative_path)?;
        let unchanged = target.exists()
            && previous
                .docs
                .get(relative_path)
                .is_some_and(|previous_meta| previous_meta == &doc_meta);

        if !unchanged {
            write_doc(out_dir, relative_path, content)?;
            generated_docs.push(relative_path.clone());
        }
        next_docs.insert(relative_path.clone(), doc_meta);
    }

    for stale_path in previous
        .docs
        .keys()
        .filter(|key| !next_docs.contains_key(*key))
    {
        let target = safe_doc_path(out_dir, stale_path)?;
        reject_symlinked_doc_path(out_dir, &target)?;
        match std::fs::remove_file(&target) {
            Ok(()) => prune_empty_doc_dirs(out_dir, &target)?,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }

    let meta = CodewikiMeta {
        docs: next_docs,
        generated_docs: generated_docs.clone(),
    };
    write_codewiki_meta(out_dir, &meta)?;
    Ok(generated_docs)
}

fn write_doc(out_dir: &Path, relative_path: &str, content: &str) -> anyhow::Result<()> {
    let target = safe_doc_path(out_dir, relative_path)?;
    reject_symlinked_doc_path(out_dir, &target)?;
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(target, content)?;
    Ok(())
}

fn reject_symlinked_doc_path(out_dir: &Path, target: &Path) -> anyhow::Result<()> {
    let relative = target.strip_prefix(out_dir)?;
    let mut current = out_dir.to_path_buf();
    for component in relative.components() {
        current.push(component);
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                anyhow::bail!(
                    "refusing to follow symlinked codewiki path: {}",
                    current.display()
                );
            }
            Ok(_) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

fn prune_empty_doc_dirs(out_dir: &Path, target: &Path) -> anyhow::Result<()> {
    let mut current = target.parent();
    while let Some(dir) = current {
        if dir == out_dir {
            break;
        }
        match std::fs::remove_dir(dir) {
            Ok(()) => current = dir.parent(),
            Err(err)
                if matches!(
                    err.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::DirectoryNotEmpty
                ) =>
            {
                break;
            }
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

fn read_codewiki_meta(out_dir: &Path) -> anyhow::Result<CodewikiMeta> {
    let path = safe_doc_path(out_dir, CODEWIKI_META_PATH)?;
    match std::fs::read_to_string(&path) {
        Ok(raw) => Ok(serde_json::from_str(&raw)?),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(CodewikiMeta::default()),
        Err(err) => Err(err.into()),
    }
}

fn write_codewiki_meta(out_dir: &Path, meta: &CodewikiMeta) -> anyhow::Result<()> {
    let content = serde_json::to_string_pretty(meta)?;
    write_doc(out_dir, CODEWIKI_META_PATH, &(content + "\n"))
}

fn source_hashes_for_doc(
    project_root: &Path,
    content: &str,
) -> anyhow::Result<BTreeMap<String, String>> {
    let mut hashes = BTreeMap::new();
    for file in source_files_from_frontmatter(content) {
        let hash = hasher::file_content_hash(&project_root.join(&file))
            .map_err(|err| anyhow::anyhow!("failed to hash codewiki source file {file}: {err}"))?;
        hashes.insert(file, hash);
    }
    Ok(hashes)
}

fn source_files_from_frontmatter(content: &str) -> BTreeSet<String> {
    let mut files = BTreeSet::new();
    let mut in_frontmatter = false;
    for line in content.lines() {
        if line == "---" {
            if in_frontmatter {
                break;
            }
            in_frontmatter = true;
            continue;
        }
        if !in_frontmatter {
            continue;
        }
        if let Some(file) = line
            .strip_prefix("  - file: ")
            .and_then(unquote_yaml_string)
        {
            files.insert(file);
        }
    }
    files
}

fn unquote_yaml_string(value: &str) -> Option<String> {
    let value = value.trim();
    let inner = value.strip_prefix('"')?.strip_suffix('"')?;
    let mut out = String::new();
    let mut chars = inner.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            out.push(chars.next()?);
        } else {
            out.push(ch);
        }
    }
    Some(out)
}

fn fetch_codewiki_graph_edges(
    ctx: &Context,
    files: &[String],
    symbols: &[Symbol],
) -> anyhow::Result<CodewikiGraph> {
    let symbol_components = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| (symbol.id.clone(), component_id(symbol)))
        .collect::<HashMap<_, _>>();
    if symbol_components.is_empty() {
        return Ok(CodewikiGraph::available(Vec::new()));
    }

    let Some(config) = &ctx.falkordb else {
        return Ok(CodewikiGraph::unavailable());
    };

    let mut client = match falkor::FalkorClient::from_config(config) {
        Ok(client) => client,
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB connection failed: {e}");
            }
            return Ok(CodewikiGraph::unavailable());
        }
    };

    fn query_or_unavailable(
        ctx: &Context,
        client: &mut falkor::FalkorClient,
        query: &str,
        params: HashMap<String, String>,
    ) -> Option<Vec<falkor::Row>> {
        match client.query(query, Some(params)) {
            Ok(rows) => Some(rows),
            Err(e) => {
                if !ctx.quiet {
                    eprintln!("Warning: FalkorDB query failed: {e}");
                }
                None
            }
        }
    }

    let symbol_ids = symbol_components.keys().cloned().collect::<Vec<_>>();
    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();

    let mut edges = Vec::new();
    let (query, params) = codewiki_call_edges_query(&ctx.project_id, &symbol_ids);
    let Some(rows) = query_or_unavailable(ctx, &mut client, &query, params) else {
        return Ok(CodewikiGraph::unavailable());
    };
    for row in rows {
        let Some(source) = row.get("source").and_then(|value| value.as_str()) else {
            continue;
        };
        let Some(target) = row.get("target").and_then(|value| value.as_str()) else {
            continue;
        };
        let Some(source_component_id) = symbol_components.get(source).cloned() else {
            continue;
        };
        let Some(target_component_id) = symbol_components.get(target).cloned() else {
            continue;
        };
        edges.push(CodewikiGraphEdge::call(
            source_component_id,
            target_component_id,
        ));
    }

    if !core_files.is_empty() {
        let file_symbols = symbols_by_file_component(symbols);
        let (query, params) = codewiki_import_edges_query(&ctx.project_id, &core_files);
        let Some(rows) = query_or_unavailable(ctx, &mut client, &query, params) else {
            return Ok(CodewikiGraph::unavailable());
        };
        for row in rows {
            let Some(source_file) = row.get("source").and_then(|value| value.as_str()) else {
                continue;
            };
            let Some(target_module) = row.get("target").and_then(|value| value.as_str()) else {
                continue;
            };
            let Some(source_component_id) = first_component_for_file(&file_symbols, source_file)
            else {
                continue;
            };
            for target_file in files_for_import_target(&core_files, target_module) {
                let Some(target_component_id) =
                    first_component_for_file(&file_symbols, target_file)
                else {
                    continue;
                };
                edges.push(CodewikiGraphEdge::import(
                    source_component_id.clone(),
                    target_component_id,
                ));
            }
        }
    }

    Ok(CodewikiGraph::available(edges))
}

fn codewiki_call_edges_query(
    project_id: &str,
    symbol_ids: &[String],
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target:CodeSymbol {{project: $project}}) \
             WHERE source.id IN [{}] AND target.id IN [{}] \
             RETURN source.id AS source, target.id AS target \
             LIMIT 5000",
            falkor::id_list_literal(symbol_ids),
            falkor::id_list_literal(symbol_ids)
        ),
        HashMap::from([(
            "project".to_string(),
            falkor::cypher_string_literal(project_id),
        )]),
    )
}

fn codewiki_import_edges_query(
    project_id: &str,
    files: &[String],
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeFile {{project: $project}})-[:IMPORTS]->(target:CodeModule {{project: $project}}) \
             WHERE source.path IN [{}] \
             RETURN source.path AS source, target.name AS target \
             LIMIT 5000",
            falkor::id_list_literal(files)
        ),
        HashMap::from([(
            "project".to_string(),
            falkor::cypher_string_literal(project_id),
        )]),
    )
}

fn cluster_file_modules(
    files: &[String],
    symbols_by_file: &BTreeMap<String, Vec<Symbol>>,
    graph_edges: &[CodewikiGraphEdge],
) -> HashMap<String, String> {
    let mut components_to_file = HashMap::new();
    for (file, symbols) in symbols_by_file {
        for symbol in symbols {
            components_to_file.insert(component_id(symbol), file.clone());
        }
    }

    let mut parents = files
        .iter()
        .map(|file| (file.clone(), file.clone()))
        .collect::<HashMap<_, _>>();
    for edge in graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Call)
    {
        let Some(source_file) = components_to_file.get(&edge.source_component_id) else {
            continue;
        };
        let Some(target_file) = components_to_file.get(&edge.target_component_id) else {
            continue;
        };
        union_files(&mut parents, source_file, target_file);
    }

    let mut grouped: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for file in files {
        let root = find_file_root(&mut parents, file);
        grouped.entry(root).or_default().push(file.clone());
    }

    let mut modules = HashMap::new();
    for grouped_files in grouped.values() {
        let module = if grouped_files.len() > 1 {
            common_module_for_files(grouped_files)
        } else {
            module_for_file(&grouped_files[0])
        };
        for file in grouped_files {
            modules.insert(file.clone(), module.clone());
        }
    }
    modules
}

fn union_files(parents: &mut HashMap<String, String>, left: &str, right: &str) {
    let left_root = find_file_root(parents, left);
    let right_root = find_file_root(parents, right);
    if left_root != right_root {
        let (parent, child) = if left_root <= right_root {
            (left_root, right_root)
        } else {
            (right_root, left_root)
        };
        parents.insert(child, parent);
    }
}

fn find_file_root(parents: &mut HashMap<String, String>, file: &str) -> String {
    let mut current = file.to_string();
    let mut path = Vec::new();
    let mut seen = HashSet::new();

    let root = loop {
        if !seen.insert(current.clone()) {
            let root = path
                .iter()
                .chain(std::iter::once(&current))
                .min()
                .cloned()
                .unwrap_or_else(|| current.clone());
            parents.insert(current, root.clone());
            break root;
        }

        let parent = parents
            .get(&current)
            .cloned()
            .unwrap_or_else(|| current.clone());
        if parent == current {
            break parent;
        }

        path.push(current);
        current = parent;
    };

    for node in path {
        parents.insert(node, root.clone());
    }
    root
}

fn common_module_for_files(files: &[String]) -> String {
    let mut common = module_for_file(&files[0])
        .split('/')
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect::<Vec<_>>();
    for file in &files[1..] {
        let parts = module_for_file(file)
            .split('/')
            .filter(|part| !part.is_empty())
            .map(str::to_string)
            .collect::<Vec<_>>();
        let keep = common
            .iter()
            .zip(parts.iter())
            .take_while(|(left, right)| left == right)
            .count();
        common.truncate(keep);
    }
    common.join("/")
}

fn symbols_by_file_component(symbols: &[Symbol]) -> BTreeMap<String, Vec<String>> {
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for symbol in symbols {
        if is_core_file(&symbol.file_path) {
            out.entry(symbol.file_path.clone())
                .or_default()
                .push(component_id(symbol));
        }
    }
    out
}

fn first_component_for_file(
    symbols_by_file: &BTreeMap<String, Vec<String>>,
    file: &str,
) -> Option<String> {
    symbols_by_file
        .get(file)
        .and_then(|components| components.first())
        .cloned()
}

fn files_for_import_target<'a>(files: &'a [String], target_module: &str) -> Vec<&'a str> {
    let target = target_module.replace("::", "/").replace('.', "/");
    files
        .iter()
        .map(String::as_str)
        .filter(|file| {
            file.starts_with(&format!("{target}/")) || file.contains(&format!("/{target}/"))
        })
        .collect()
}

fn build_file_doc(
    file: &str,
    module: String,
    symbols: Vec<Symbol>,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> FileDoc {
    let symbol_docs = symbols
        .into_iter()
        .map(|symbol| {
            let fallback = structural_symbol_purpose(&symbol);
            let generated = maybe_generate(
                generate,
                &prompts::symbol_prompt(&symbol),
                prompts::SYMBOL_SYSTEM,
            )
            .unwrap_or(fallback);
            let component_id = component_id(&symbol);
            let component_label = component_label(&symbol);
            let source_span = SourceSpan::from_symbol(&symbol);
            let purpose = ground_text(
                &generated,
                std::slice::from_ref(&source_span),
                &source_span.citation(),
            );
            SymbolDoc {
                symbol,
                purpose,
                component_id,
                component_label,
                source_span,
            }
        })
        .collect::<Vec<_>>();
    let source_spans = symbol_docs
        .iter()
        .map(|symbol| symbol.source_span.clone())
        .collect::<Vec<_>>();
    let prompt_symbols = symbol_docs
        .iter()
        .map(|symbol| prompts::SymbolSummary {
            name: symbol.symbol.qualified_name.clone(),
            kind: symbol.symbol.kind.clone(),
            component_id: symbol.component_id.clone(),
            component_label: symbol.component_label.clone(),
            line_start: symbol.symbol.line_start,
            line_end: symbol.symbol.line_end,
            purpose: symbol.purpose.clone(),
        })
        .collect::<Vec<_>>();
    let component_ids = symbol_docs
        .iter()
        .map(|symbol| symbol.component_id.clone())
        .collect::<Vec<_>>();
    let fallback = structural_file_summary(file, &symbol_docs);
    let generated = maybe_generate(
        generate,
        &prompts::file_prompt(file, &prompt_symbols),
        prompts::FILE_SYSTEM,
    )
    .unwrap_or(fallback);
    let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

    FileDoc {
        path: file.to_string(),
        module,
        summary,
        source_spans,
        symbols: symbol_docs,
        component_ids,
    }
}

fn build_module_docs(
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
    graph_availability: CodewikiGraphAvailability,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> Vec<ModuleDoc> {
    let mut module_names = BTreeSet::new();
    for file in files {
        for module in module_ancestors(&file.module) {
            module_names.insert(module);
        }
    }

    let mut module_summaries: BTreeMap<String, String> = BTreeMap::new();
    let mut module_sources: BTreeMap<String, Vec<SourceSpan>> = BTreeMap::new();
    let mut modules = module_names.into_iter().collect::<Vec<_>>();
    modules.sort_by_key(|module| std::cmp::Reverse(module_depth(module)));

    let mut docs = Vec::new();
    for module in modules {
        let direct_files = files
            .iter()
            .filter(|file| file.module == module)
            .map(|file| FileLink {
                path: file.path.clone(),
                summary: file.summary.clone(),
                source_spans: file.source_spans.clone(),
            })
            .collect::<Vec<_>>();
        let child_modules = direct_child_modules(&module, module_summaries.keys())
            .into_iter()
            .map(|child| ModuleLink {
                summary: module_summaries.get(&child).cloned().unwrap_or_default(),
                source_spans: module_sources.get(&child).cloned().unwrap_or_default(),
                module: child,
            })
            .collect::<Vec<_>>();
        let file_summaries = direct_files
            .iter()
            .map(|file| prompts::ChildSummary {
                name: file.path.clone(),
                summary: file.summary.clone(),
            })
            .collect::<Vec<_>>();
        let child_summaries = child_modules
            .iter()
            .map(|module| prompts::ChildSummary {
                name: module.module.clone(),
                summary: module.summary.clone(),
            })
            .collect::<Vec<_>>();
        let component_ids = files
            .iter()
            .filter(|file| file.module == module || module_is_ancestor(&module, &file.module))
            .flat_map(|file| {
                file.symbols
                    .iter()
                    .map(|symbol| format!("{} ({})", symbol.component_label, symbol.component_id))
            })
            .collect::<Vec<_>>();
        let dependency_diagram = render_module_dependency_mermaid(&module, files, graph_edges);
        let call_diagram = render_module_call_mermaid(&module, files, graph_edges);
        let fallback = structural_module_summary(&module, &direct_files, &child_modules);
        let source_spans = collect_link_spans(&direct_files, &child_modules);
        let generated = maybe_generate(
            generate,
            &prompts::module_prompt(&module, &file_summaries, &child_summaries, &component_ids),
            prompts::MODULE_SYSTEM,
        )
        .unwrap_or(fallback);
        let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

        module_summaries.insert(module.clone(), summary.clone());
        module_sources.insert(module.clone(), source_spans.clone());
        docs.push(ModuleDoc {
            module,
            summary,
            source_spans,
            direct_files,
            child_modules,
            component_ids,
            dependency_diagram,
            call_diagram,
            graph_availability,
        });
    }

    docs.sort_by(|a, b| a.module.cmp(&b.module));
    docs
}

fn render_module_dependency_mermaid(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let mut component_to_module = HashMap::new();
    for file in files {
        for component_id in &file.component_ids {
            component_to_module.insert(component_id.as_str(), file.module.as_str());
        }
    }

    let all_edges = graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Import)
        .filter_map(|edge| {
            let source = component_to_module.get(edge.source_component_id.as_str())?;
            let target = component_to_module.get(edge.target_component_id.as_str())?;
            if source == target {
                return None;
            }
            Some(((*source).to_string(), (*target).to_string()))
        })
        .collect::<BTreeSet<_>>();
    if all_edges.is_empty() {
        return None;
    }

    let bounded_edges = bounded_module_dependency_edges(module, &all_edges, MAX_MERMAID_HOPS);
    if bounded_edges.is_empty() {
        return None;
    }

    let mut diagram = "```mermaid\ngraph LR\n".to_string();
    for (source, target) in bounded_edges {
        let _ = writeln!(
            diagram,
            "    {}[\"{}\"] --> {}[\"{}\"]",
            mermaid_node_id(&source),
            mermaid_label(&source),
            mermaid_node_id(&target),
            mermaid_label(&target)
        );
    }
    diagram.push_str("```\n");
    Some(diagram)
}

fn render_module_call_mermaid(
    module: &str,
    files: &[FileDoc],
    graph_edges: &[CodewikiGraphEdge],
) -> Option<String> {
    let component_labels = files
        .iter()
        .flat_map(|file| {
            file.symbols.iter().map(|symbol| {
                (
                    symbol.component_id.as_str(),
                    symbol.component_label.as_str(),
                )
            })
        })
        .collect::<HashMap<_, _>>();
    let component_to_module = files
        .iter()
        .flat_map(|file| {
            file.component_ids
                .iter()
                .map(|component_id| (component_id.as_str(), file.module.as_str()))
        })
        .collect::<HashMap<_, _>>();
    let all_edges = graph_edges
        .iter()
        .filter(|edge| edge.kind == CodewikiGraphEdgeKind::Call)
        .filter_map(|edge| {
            let source_module = component_to_module.get(edge.source_component_id.as_str())?;
            let target_module = component_to_module.get(edge.target_component_id.as_str())?;
            if *source_module != module && *target_module != module {
                return None;
            }
            Some((
                edge.source_component_id.clone(),
                edge.target_component_id.clone(),
            ))
        })
        .collect::<BTreeSet<_>>();
    if all_edges.is_empty() {
        return None;
    }

    let seed_components = files
        .iter()
        .filter(|file| file.module == module || module_is_ancestor(module, &file.module))
        .flat_map(|file| file.component_ids.iter().cloned())
        .collect::<BTreeSet<_>>();
    let bounded_edges = bounded_component_edges(
        &seed_components,
        &all_edges,
        MAX_MERMAID_HOPS,
        MAX_MERMAID_EDGES,
    );
    if bounded_edges.is_empty() {
        return None;
    }

    let mut participants = BTreeSet::new();
    for (source, target) in &bounded_edges {
        participants.insert(source.clone());
        participants.insert(target.clone());
    }

    let mut diagram = "```mermaid\nsequenceDiagram\n".to_string();
    for component in participants {
        let _ = writeln!(
            diagram,
            "    participant {} as {}",
            mermaid_node_id(&component),
            mermaid_label(
                component_labels
                    .get(component.as_str())
                    .copied()
                    .unwrap_or(&component)
            )
        );
    }
    for (source, target) in bounded_edges {
        let _ = writeln!(
            diagram,
            "    {}->>{}: calls",
            mermaid_node_id(&source),
            mermaid_node_id(&target)
        );
    }
    diagram.push_str("```\n");
    Some(diagram)
}

fn bounded_module_dependency_edges(
    module: &str,
    edges: &BTreeSet<(String, String)>,
    max_hops: usize,
) -> BTreeSet<(String, String)> {
    let mut distances = BTreeMap::from([(module.to_string(), 0usize)]);
    let mut queue = VecDeque::from([(module.to_string(), 0usize)]);

    while let Some((current, distance)) = queue.pop_front() {
        if distance >= max_hops {
            continue;
        }
        for (source, target) in edges {
            for next in dependency_neighbors(&current, source, target) {
                if distances.contains_key(next) {
                    continue;
                }
                let next_distance = distance + 1;
                distances.insert(next.to_string(), next_distance);
                queue.push_back((next.to_string(), next_distance));
            }
        }
    }

    edges
        .iter()
        .filter(|(source, target)| distances.contains_key(source) && distances.contains_key(target))
        .cloned()
        .collect()
}

fn bounded_component_edges(
    seed_components: &BTreeSet<String>,
    edges: &BTreeSet<(String, String)>,
    max_hops: usize,
    max_edges: usize,
) -> BTreeSet<(String, String)> {
    let mut distances = seed_components
        .iter()
        .map(|component| (component.clone(), 0usize))
        .collect::<BTreeMap<_, _>>();
    let mut queue = seed_components
        .iter()
        .map(|component| (component.clone(), 0usize))
        .collect::<VecDeque<_>>();

    while let Some((current, distance)) = queue.pop_front() {
        if distance >= max_hops {
            continue;
        }
        for (source, target) in edges {
            for next in dependency_neighbors(&current, source, target) {
                if distances.contains_key(next) {
                    continue;
                }
                let next_distance = distance + 1;
                distances.insert(next.to_string(), next_distance);
                queue.push_back((next.to_string(), next_distance));
            }
        }
    }

    edges
        .iter()
        .filter(|(source, target)| distances.contains_key(source) && distances.contains_key(target))
        .take(max_edges)
        .cloned()
        .collect()
}

fn dependency_neighbors<'a>(module: &str, source: &'a str, target: &'a str) -> Vec<&'a str> {
    let mut neighbors = Vec::with_capacity(2);
    if source == module {
        neighbors.push(target);
    }
    if target == module {
        neighbors.push(source);
    }
    neighbors
}

fn mermaid_node_id(module: &str) -> String {
    let mut out = String::from("m_");
    for ch in module.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    out
}

fn mermaid_label(module: &str) -> String {
    if module.is_empty() {
        "repo".to_string()
    } else {
        module.replace('\\', "\\\\").replace('"', "\\\"")
    }
}

fn build_repo_doc(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> String {
    let top_modules = modules
        .iter()
        .filter(|module| parent_module(&module.module).is_none())
        .map(|module| ModuleLink {
            module: module.module.clone(),
            summary: module.summary.clone(),
            source_spans: module.source_spans.clone(),
        })
        .collect::<Vec<_>>();
    let root_files = files
        .iter()
        .filter(|file| file.module.is_empty())
        .map(|file| FileLink {
            path: file.path.clone(),
            summary: file.summary.clone(),
            source_spans: file.source_spans.clone(),
        })
        .collect::<Vec<_>>();
    let module_summaries = top_modules
        .iter()
        .map(|module| prompts::ChildSummary {
            name: module.module.clone(),
            summary: module.summary.clone(),
        })
        .collect::<Vec<_>>();
    let file_summaries = root_files
        .iter()
        .map(|file| prompts::ChildSummary {
            name: file.path.clone(),
            summary: file.summary.clone(),
        })
        .collect::<Vec<_>>();
    let fallback = structural_repo_summary(files.len(), modules.len());
    let source_spans = collect_link_spans(&root_files, &top_modules);
    let generated = maybe_generate(
        generate,
        &prompts::repo_prompt(&module_summaries, &file_summaries),
        prompts::REPO_SYSTEM,
    )
    .unwrap_or(fallback);
    let summary = ground_text(&generated, &source_spans, &citation_list(&source_spans));

    render_repo_doc(&summary, &top_modules, &root_files, &source_spans)
}

fn render_repo_doc(
    summary: &str,
    modules: &[ModuleLink],
    files: &[FileLink],
    source_spans: &[SourceSpan],
) -> String {
    let mut doc = frontmatter("Repository Overview", "code_repo", source_spans);
    doc.push_str("# Repository Overview\n\n");
    write_section(&mut doc, "Overview", summary);
    if !modules.is_empty() {
        doc.push_str("## Modules\n\n");
        for module in modules {
            let _ = writeln!(
                doc,
                "- {} - {}",
                module_wikilink(&module.module),
                module.summary
            );
        }
        doc.push('\n');
    }
    if !files.is_empty() {
        doc.push_str("## Files\n\n");
        for file in files {
            let _ = writeln!(doc, "- {} - {}", file_wikilink(&file.path), file.summary);
        }
        doc.push('\n');
    }
    doc
}

fn render_module_doc(module: &ModuleDoc) -> String {
    let mut doc = frontmatter(&module.module, "code_module", &module.source_spans);
    let _ = writeln!(doc, "# {}\n", module.module);
    match parent_module(&module.module) {
        Some(parent) => {
            let _ = writeln!(doc, "Parent: {}\n", module_wikilink(parent));
        }
        None => doc.push_str("Parent: [[repo|Repository Overview]]\n\n"),
    }
    write_section(&mut doc, "Overview", &module.summary);
    match module.graph_availability {
        CodewikiGraphAvailability::Unavailable => {
            doc.push_str("## Dependency Diagram\n\n`degraded: graph-unavailable`\n\n");
        }
        CodewikiGraphAvailability::Available => {
            if let Some(diagram) = &module.dependency_diagram {
                doc.push_str("## Dependency Diagram\n\n");
                doc.push_str(diagram);
                doc.push('\n');
            }
            if let Some(diagram) = &module.call_diagram {
                doc.push_str("## Call Diagram\n\n");
                doc.push_str(diagram);
                doc.push('\n');
            }
        }
    }
    if !module.child_modules.is_empty() {
        doc.push_str("## Child Modules\n\n");
        for child in &module.child_modules {
            let _ = writeln!(
                doc,
                "- {} - {}",
                module_wikilink(&child.module),
                child.summary
            );
        }
        doc.push('\n');
    }
    if !module.direct_files.is_empty() {
        doc.push_str("## Files\n\n");
        for file in &module.direct_files {
            let _ = writeln!(doc, "- {} - {}", file_wikilink(&file.path), file.summary);
        }
        doc.push('\n');
    }
    if !module.component_ids.is_empty() {
        doc.push_str("## Components\n\n");
        for component_id in &module.component_ids {
            let _ = writeln!(doc, "- {}", inline_code(component_id));
        }
        doc.push('\n');
    }
    doc
}

fn render_file_doc(file: &FileDoc) -> String {
    let mut doc = frontmatter(&file.path, "code_file", &file.source_spans);
    let _ = writeln!(doc, "# {}\n", file.path);
    if file.module.is_empty() {
        doc.push_str("Module: [[repo|Repository Overview]]\n\n");
    } else {
        let _ = writeln!(doc, "Module: {}\n", module_wikilink(&file.module));
    }
    write_section(&mut doc, "Purpose", &file.summary);
    doc.push_str("## API Symbols\n\n");
    if file.symbols.is_empty() {
        doc.push_str("No indexed symbols.\n");
        return doc;
    }
    for symbol in &file.symbols {
        let _ = writeln!(
            doc,
            "- {} ({}) component {} ({}) lines {}-{} {}",
            inline_code(&symbol.symbol.qualified_name),
            symbol.symbol.kind,
            inline_code(&symbol.component_label),
            inline_code(&symbol.component_id),
            symbol.symbol.line_start,
            symbol.symbol.line_end,
            symbol.source_span.citation()
        );
        if let Some(signature) = symbol
            .symbol
            .signature
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            let _ = writeln!(doc, "  - Signature: {}", inline_code(signature));
        }
        let _ = writeln!(doc, "  - Purpose: {}", symbol.purpose);
    }
    doc.push('\n');
    doc
}

fn resolve_text_generator(
    ctx: &Context,
    ai: Option<AiRouting>,
) -> Option<Box<TextGenerator<'static>>> {
    let ai_context = resolve_ai_context(ctx, ai).ok()?;
    let route = effective_route(&ai_context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return None;
    }

    let mut warned = false;
    let quiet = ctx.quiet;
    Some(Box::new(move |prompt, system| {
        let result = match route {
            AiRouting::Daemon => generate_via_daemon(&ai_context, prompt, Some(system)),
            AiRouting::Direct => generate_text(&ai_context, prompt, Some(system)),
            AiRouting::Off | AiRouting::Auto => return None,
        };
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned {
                    eprintln!("text generation unavailable; using AST-only codewiki docs: {error}");
                    warned = true;
                }
                None
            }
        }
    }))
}

fn resolve_ai_context(ctx: &Context, ai: Option<AiRouting>) -> anyhow::Result<AiContext> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    Ok(AiContext::resolve_with_options(
        Some(ctx.project_id.clone()),
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: ai,
        },
    ))
}

fn maybe_generate(
    generate: &mut Option<&mut TextGenerator<'_>>,
    prompt: &str,
    system: &str,
) -> Option<String> {
    generate
        .as_deref_mut()
        .and_then(|generate| generate(prompt, system))
        .and_then(clean_generated)
}

fn clean_generated(text: String) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_string())
}

fn structural_symbol_purpose(symbol: &Symbol) -> String {
    if let Some(summary) = symbol.summary.as_deref().filter(|value| !value.is_empty()) {
        return summary.to_string();
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        return docstring.to_string();
    }
    format!(
        "Indexed {} `{}` in `{}`.",
        symbol.kind, symbol.qualified_name, symbol.file_path
    )
}

fn structural_file_summary(file: &str, symbols: &[SymbolDoc]) -> String {
    if symbols.is_empty() {
        return format!("`{file}` has no indexed API symbols.");
    }
    format!(
        "`{file}` exposes {} indexed API symbol{}.",
        symbols.len(),
        plural(symbols.len())
    )
}

fn structural_module_summary(
    module: &str,
    files: &[FileLink],
    child_modules: &[ModuleLink],
) -> String {
    let file_count = files.len();
    let child_count = child_modules.len();
    format!(
        "`{module}` contains {file_count} direct file{} and {child_count} child module{}.",
        plural(file_count),
        plural(child_count)
    )
}

fn structural_repo_summary(file_count: usize, module_count: usize) -> String {
    format!(
        "Repository code documentation covers {file_count} file{} across {module_count} module{}.",
        plural(file_count),
        plural(module_count)
    )
}

fn write_section(doc: &mut String, heading: &str, body: &str) {
    let _ = writeln!(doc, "## {heading}\n\n{}\n", body.trim());
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

fn collect_link_spans(files: &[FileLink], modules: &[ModuleLink]) -> Vec<SourceSpan> {
    let mut spans = BTreeSet::new();
    for file in files {
        spans.extend(file.source_spans.iter().cloned());
    }
    for module in modules {
        spans.extend(module.source_spans.iter().cloned());
    }
    spans.into_iter().collect()
}

fn citation_list(spans: &[SourceSpan]) -> String {
    spans
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(|span| span.citation())
        .collect::<Vec<_>>()
        .join(" ")
}

fn ground_text(text: &str, valid_spans: &[SourceSpan], fallback_citation: &str) -> String {
    let cleaned = strip_invalid_citations(text, valid_spans);
    if fallback_citation.is_empty() || contains_valid_citation(&cleaned, valid_spans) {
        cleaned
    } else {
        format!("{cleaned} {fallback_citation}")
    }
}

fn strip_invalid_citations(text: &str, valid_spans: &[SourceSpan]) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let (before, after_open) = rest.split_at(open);
        out.push_str(before);
        let after_open = &after_open[1..];
        let Some(close) = after_open.find(']') else {
            out.push('[');
            out.push_str(after_open);
            return out;
        };
        let candidate = &after_open[..close];
        if citation_parts(candidate).is_none_or(|(file, start, end)| {
            valid_spans
                .iter()
                .any(|span| span.contains(file, start, end))
        }) {
            out.push('[');
            out.push_str(candidate);
            out.push(']');
        }
        rest = &after_open[close + 1..];
    }
    out.push_str(rest);
    out
}

fn contains_valid_citation(text: &str, valid_spans: &[SourceSpan]) -> bool {
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let after_open = &rest[open + 1..];
        let Some(close) = after_open.find(']') else {
            return false;
        };
        if let Some((file, start, end)) = citation_parts(&after_open[..close])
            && valid_spans
                .iter()
                .any(|span| span.contains(file, start, end))
        {
            return true;
        }
        rest = &after_open[close + 1..];
    }
    false
}

fn citation_parts(value: &str) -> Option<(&str, usize, usize)> {
    let (file, range) = value.rsplit_once(':')?;
    if file.is_empty() || file.chars().any(char::is_whitespace) {
        return None;
    }
    let (line_start, line_end) = match range.split_once('-') {
        Some((start, end)) => (start.parse().ok()?, end.parse().ok()?),
        None => {
            let line = range.parse().ok()?;
            (line, line)
        }
    };
    (line_start > 0 && line_start <= line_end).then_some((file, line_start, line_end))
}

fn frontmatter(title: &str, kind: &str, source_spans: &[SourceSpan]) -> String {
    let mut out = format!("---\ntitle: \"{}\"\ntype: {kind}\n", yaml_quote(title));
    let mut files: BTreeMap<&str, BTreeSet<(usize, usize)>> = BTreeMap::new();
    for span in source_spans {
        files
            .entry(&span.file)
            .or_default()
            .insert((span.line_start, span.line_end));
    }
    if files.is_empty() {
        out.push_str("source_files: []\n");
        out.push_str("---\n\n");
        return out;
    }
    out.push_str("source_files:\n");
    for (file, ranges) in files {
        let _ = writeln!(out, "  - file: \"{}\"", yaml_quote(file));
        out.push_str("    ranges:\n");
        for (line_start, line_end) in ranges {
            if line_start == line_end {
                let _ = writeln!(out, "      - \"{line_start}\"");
            } else {
                let _ = writeln!(out, "      - \"{line_start}-{line_end}\"");
            }
        }
    }
    out.push_str("---\n\n");
    out
}

fn yaml_quote(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn inline_code(value: &str) -> String {
    let value = value.replace('\n', " ");
    let delimiter = "`".repeat(max_backtick_run(&value).saturating_add(1));
    if value.starts_with('`') || value.ends_with('`') {
        format!("{delimiter} {value} {delimiter}")
    } else {
        format!("{delimiter}{value}{delimiter}")
    }
}

fn max_backtick_run(value: &str) -> usize {
    let mut max_run = 0usize;
    let mut current_run = 0usize;
    for ch in value.chars() {
        if ch == '`' {
            current_run += 1;
            max_run = max_run.max(current_run);
        } else {
            current_run = 0;
        }
    }
    max_run
}

fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
}

fn component_id(symbol: &Symbol) -> String {
    symbol.id.clone()
}

fn component_label(symbol: &Symbol) -> String {
    let name = if symbol.qualified_name.is_empty() {
        &symbol.name
    } else {
        &symbol.qualified_name
    };
    format!("{name} [{}]", symbol.kind)
}

fn is_core_file(file: &str) -> bool {
    let lower = file.to_ascii_lowercase();
    if lower.contains(".generated.")
        || lower.ends_with(".generated.rs")
        || lower.ends_with(".gen.rs")
        || lower.contains(".test.")
        || lower.contains(".spec.")
        || lower.ends_with("_test.rs")
        || lower.ends_with("_tests.rs")
    {
        return false;
    }
    !Path::new(file).components().any(|component| {
        let part = component.as_os_str().to_string_lossy().to_ascii_lowercase();
        matches!(
            part.as_str(),
            "test"
                | "tests"
                | "__tests__"
                | "spec"
                | "specs"
                | "fixture"
                | "fixtures"
                | "vendor"
                | "vendored"
                | "third_party"
                | "generated"
                | "gen"
                | "dist"
                | "build"
                | "target"
                | "node_modules"
        )
    })
}

fn in_scope(file: &str, scopes: &[String]) -> bool {
    scopes.is_empty()
        || scopes.iter().any(|scope| scope.is_empty())
        || scopes.iter().any(|scope| {
            file == scope || file.starts_with(&format!("{}/", scope.trim_end_matches('/')))
        })
}

fn module_for_file(file: &str) -> String {
    Path::new(file)
        .parent()
        .map(|path| path.to_string_lossy().replace('\\', "/"))
        .filter(|path| path != ".")
        .unwrap_or_default()
}

fn module_ancestors(module: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = module;
    while !current.is_empty() {
        out.push(current.to_string());
        current = parent_module(current).unwrap_or("");
    }
    out
}

fn parent_module(module: &str) -> Option<&str> {
    module.rsplit_once('/').map(|(parent, _)| parent)
}

fn module_is_ancestor(module: &str, child: &str) -> bool {
    !module.is_empty() && child.starts_with(&format!("{module}/"))
}

fn direct_child_modules<'a>(
    module: &str,
    candidates: impl Iterator<Item = &'a String>,
) -> Vec<String> {
    candidates
        .filter(|candidate| parent_module(candidate).is_some_and(|parent| parent == module))
        .cloned()
        .collect()
}

fn module_depth(module: &str) -> usize {
    module.split('/').count()
}

fn file_doc_path(file: &str) -> String {
    format!("files/{file}.md")
}

fn module_doc_path(module: &str) -> String {
    format!("modules/{module}.md")
}

fn file_wikilink(file: &str) -> String {
    format!("[[files/{file}|{file}]]")
}

fn module_wikilink(module: &str) -> String {
    format!("[[modules/{module}|{module}]]")
}

fn safe_doc_path(out_dir: &Path, relative_path: &str) -> anyhow::Result<PathBuf> {
    let path = Path::new(relative_path);
    if path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        anyhow::bail!("refusing to write unsafe codewiki path: {relative_path}");
    }
    Ok(out_dir.join(path))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generates_hierarchical_docs() {
        let out_dir = tempfile::tempdir().expect("tempdir");
        let input = CodewikiInput {
            files: vec!["src/lib.rs".to_string(), "src/nested/api.rs".to_string()],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![
                test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client {"),
                test_symbol("src/lib.rs", "connect", "function", 5, "pub fn connect()"),
                test_symbol(
                    "src/nested/api.rs",
                    "serve",
                    "function",
                    3,
                    "pub fn serve()",
                ),
            ],
        };

        let docs = generate_hierarchical_docs(&input, None);
        write_doc_set(out_dir.path(), &docs).expect("writes docs");

        let repo = std::fs::read_to_string(out_dir.path().join("repo.md")).expect("repo doc");
        let module =
            std::fs::read_to_string(out_dir.path().join("modules/src.md")).expect("src module doc");
        let file =
            std::fs::read_to_string(out_dir.path().join("files/src/lib.rs.md")).expect("file doc");

        assert!(repo.contains("[[modules/src|src]]"));
        assert!(repo.contains("Repository Overview"));
        assert!(module.contains("[[files/src/lib.rs|src/lib.rs]]"));
        assert!(file.contains("API Symbols"));
        assert!(file.contains("pub struct Client {"));
        assert!(file.contains("[[modules/src|src]]"));
    }

    #[test]
    fn inline_code_uses_commonmark_backtick_delimiters() {
        assert_eq!(inline_code("plain"), "`plain`");
        assert_eq!(inline_code("a`b"), "``a`b``");
        assert_eq!(inline_code("a``b"), "```a``b```");
        assert_eq!(inline_code("`edge`"), "`` `edge` ``");
        assert_eq!(inline_code("two\nlines"), "`two lines`");
    }

    #[test]
    fn clusters_modules_from_graph() {
        let input = CodewikiInput {
            files: vec![
                "src/api/handler.rs".to_string(),
                "src/domain/service.rs".to_string(),
                "tests/domain/service_test.rs".to_string(),
                "vendor/generated/client.rs".to_string(),
            ],
            graph_edges: vec![CodewikiGraphEdge::call(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/domain/service.rs", "Service", "class"),
            )],
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![
                test_symbol(
                    "src/api/handler.rs",
                    "handle",
                    "function",
                    1,
                    "pub fn handle()",
                ),
                test_symbol(
                    "src/domain/service.rs",
                    "Service",
                    "class",
                    1,
                    "pub struct Service;",
                ),
                test_symbol_with_qualified(
                    "src/domain/service.rs",
                    "new",
                    "Service::new",
                    "function",
                    3,
                    "pub fn new() -> Self",
                ),
                test_symbol(
                    "tests/domain/service_test.rs",
                    "service_test",
                    "function",
                    1,
                    "fn service_test()",
                ),
                test_symbol(
                    "vendor/generated/client.rs",
                    "GeneratedClient",
                    "class",
                    1,
                    "pub struct GeneratedClient;",
                ),
            ],
        };

        let docs = generate_hierarchical_docs(&input, None);
        let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();

        let module = docs_by_path
            .get("modules/src.md")
            .expect("graph-connected files cluster under common module");
        assert!(module.contains("[[files/src/api/handler.rs|src/api/handler.rs]]"));
        assert!(module.contains("[[files/src/domain/service.rs|src/domain/service.rs]]"));
        assert!(module.contains(&test_component_id(
            "src/api/handler.rs",
            "handle",
            "function"
        )));
        assert!(module.contains(&test_component_id(
            "src/domain/service.rs",
            "Service",
            "class"
        )));
        assert!(!docs_by_path.contains_key("files/tests/domain/service_test.rs.md"));
        assert!(!docs_by_path.contains_key("files/vendor/generated/client.rs.md"));
    }

    #[test]
    fn file_root_detection_breaks_parent_cycles() {
        let mut parents = HashMap::from([
            ("b.rs".to_string(), "a.rs".to_string()),
            ("a.rs".to_string(), "b.rs".to_string()),
        ]);

        let root = find_file_root(&mut parents, "a.rs");

        assert_eq!(root, "a.rs");
        assert_eq!(parents.get("a.rs").map(String::as_str), Some("a.rs"));
        assert_eq!(parents.get("b.rs").map(String::as_str), Some("a.rs"));
    }

    #[test]
    fn clusters_without_falkordb() {
        let input = CodewikiInput {
            files: vec![
                "src/api/handler.rs".to_string(),
                "src/domain/service.rs".to_string(),
                "tests/domain/service_test.rs".to_string(),
            ],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Unavailable,
            symbols: vec![
                test_symbol(
                    "src/api/handler.rs",
                    "handle",
                    "function",
                    1,
                    "pub fn handle()",
                ),
                test_symbol(
                    "src/domain/service.rs",
                    "Service",
                    "class",
                    1,
                    "pub struct Service;",
                ),
                test_symbol_with_qualified(
                    "src/domain/service.rs",
                    "new",
                    "Service::new",
                    "function",
                    3,
                    "pub fn new() -> Self",
                ),
                test_symbol(
                    "tests/domain/service_test.rs",
                    "service_test",
                    "function",
                    1,
                    "fn service_test()",
                ),
            ],
        };

        let docs = generate_hierarchical_docs(&input, None);
        let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();

        assert!(docs_by_path.contains_key("modules/src/api.md"));
        assert!(docs_by_path.contains_key("modules/src/domain.md"));
        assert!(!docs_by_path.contains_key("files/tests/domain/service_test.rs.md"));
        assert!(
            docs_by_path
                .get("files/src/api/handler.rs.md")
                .expect("handler file doc")
                .contains(&test_component_id(
                    "src/api/handler.rs",
                    "handle",
                    "function"
                ))
        );
        assert!(
            docs_by_path
                .get("files/src/domain/service.rs.md")
                .expect("service file doc")
                .contains(&test_component_id(
                    "src/domain/service.rs",
                    "Service",
                    "class"
                ))
        );
        assert!(
            docs_by_path
                .get("files/src/domain/service.rs.md")
                .expect("service file doc")
                .contains(&test_component_id(
                    "src/domain/service.rs",
                    "new",
                    "function"
                ))
        );
        assert!(
            !docs_by_path
                .get("files/src/domain/service.rs.md")
                .expect("service file doc")
                .contains("src/domain/service.rs::Service::new")
        );
    }

    #[test]
    fn emits_bounded_mermaid() {
        let input = CodewikiInput {
            files: vec![
                "src/api/handler.rs".to_string(),
                "src/domain/service.rs".to_string(),
                "src/storage/repo.rs".to_string(),
                "src/unrelated/tool.rs".to_string(),
            ],
            graph_edges: vec![
                CodewikiGraphEdge::import(
                    test_component_id("src/api/handler.rs", "handle", "function"),
                    test_component_id("src/domain/service.rs", "Service", "class"),
                ),
                CodewikiGraphEdge::import(
                    test_component_id("src/domain/service.rs", "Service", "class"),
                    test_component_id("src/storage/repo.rs", "Repo", "class"),
                ),
                CodewikiGraphEdge::import(
                    test_component_id("src/unrelated/tool.rs", "Tool", "class"),
                    test_component_id("src/storage/repo.rs", "Repo", "class"),
                ),
            ],
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![
                test_symbol(
                    "src/api/handler.rs",
                    "handle",
                    "function",
                    1,
                    "pub fn handle()",
                ),
                test_symbol(
                    "src/domain/service.rs",
                    "Service",
                    "class",
                    1,
                    "pub struct Service;",
                ),
                test_symbol(
                    "src/storage/repo.rs",
                    "Repo",
                    "class",
                    1,
                    "pub struct Repo;",
                ),
                test_symbol(
                    "src/unrelated/tool.rs",
                    "Tool",
                    "class",
                    1,
                    "pub struct Tool;",
                ),
            ],
        };

        let docs = generate_hierarchical_docs(&input, None);
        let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
        let rendered = docs_by_path
            .get("modules/src/api.md")
            .expect("api module doc");

        assert!(rendered.contains("```mermaid"));
        assert!(rendered.contains("graph LR"));
        assert!(rendered.contains("m_src_api[\"src/api\"] --> m_src_domain[\"src/domain\"]"));
        assert!(
            rendered.contains("m_src_domain[\"src/domain\"] --> m_src_storage[\"src/storage\"]")
        );
        assert!(
            !rendered
                .contains("m_src_unrelated[\"src/unrelated\"] --> m_src_storage[\"src/storage\"]")
        );
    }

    #[test]
    fn mermaid_degrades_without_falkordb() {
        let input = CodewikiInput {
            files: vec!["src/api/handler.rs".to_string()],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Unavailable,
            symbols: vec![test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            )],
        };

        let docs = generate_hierarchical_docs(&input, None);
        let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
        let module = docs_by_path
            .get("modules/src/api.md")
            .expect("module doc still renders");
        let file = docs_by_path
            .get("files/src/api/handler.rs.md")
            .expect("file doc still renders");

        assert!(module.contains("degraded: graph-unavailable"));
        assert!(file.contains("API Symbols"));
        assert!(file.contains(&test_component_id(
            "src/api/handler.rs",
            "handle",
            "function"
        )));
    }

    #[test]
    fn empty_available_graph_does_not_emit_degradation_marker() {
        let input = CodewikiInput {
            files: vec!["src/api/handler.rs".to_string()],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            )],
        };

        let docs = generate_hierarchical_docs(&input, None);
        let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
        let module = docs_by_path
            .get("modules/src/api.md")
            .expect("module doc still renders");

        assert!(!module.contains("degraded: graph-unavailable"));
    }

    #[test]
    fn citations_validated_against_spans() {
        let input = CodewikiInput {
            files: vec!["src/lib.rs".to_string()],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![
                test_symbol_range(
                    "src/lib.rs",
                    "Client",
                    "class",
                    10,
                    14,
                    "pub struct Client {",
                ),
                test_symbol_range(
                    "src/lib.rs",
                    "connect",
                    "function",
                    20,
                    24,
                    "pub fn connect()",
                ),
            ],
        };
        let mut generator = |prompt: &str, _system: &str| {
            if prompt.contains("Client") {
                Some("Builds client state [src/lib.rs:999].".to_string())
            } else if prompt.contains("connect") {
                Some("Opens a connection [src/lib.rs:20].".to_string())
            } else {
                Some("Coordinates the public API [missing.rs:1].".to_string())
            }
        };

        let docs = generate_hierarchical_docs(&input, Some(&mut generator));
        let file_doc = docs
            .iter()
            .find(|(path, _)| path == "files/src/lib.rs.md")
            .map(|(_, content)| content)
            .expect("file doc");

        assert!(file_doc.contains("source_files:\n"));
        assert!(file_doc.contains("  - file: \"src/lib.rs\"\n"));
        assert!(file_doc.contains("    ranges:\n"));
        assert!(file_doc.contains("      - \"10-14\"\n"));
        assert!(file_doc.contains("      - \"20-24\"\n"));
        assert!(file_doc.contains("[src/lib.rs:10-14]"));
        assert!(file_doc.contains("[src/lib.rs:20]"));
        assert!(!file_doc.contains("src/lib.rs:999"));
        assert!(!file_doc.contains("missing.rs:1"));
    }

    #[test]
    fn incremental_regenerates_only_changed() {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src/nested")).expect("source dirs");
        std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n")
            .expect("write lib");
        std::fs::write(
            project.path().join("src/nested/api.rs"),
            "pub fn serve() {}\n",
        )
        .expect("write api");
        let out_dir = project.path().join("codewiki");

        let input = CodewikiInput {
            files: vec!["src/lib.rs".to_string(), "src/nested/api.rs".to_string()],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![
                test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
                test_symbol(
                    "src/nested/api.rs",
                    "serve",
                    "function",
                    1,
                    "pub fn serve()",
                ),
            ],
        };

        let first_docs = generate_hierarchical_docs(&input, None);
        let first_written =
            write_incremental_doc_set(project.path(), &out_dir, &first_docs).expect("first write");
        assert!(first_written.contains(&"repo.md".to_string()));
        assert!(first_written.contains(&"modules/src.md".to_string()));
        assert!(first_written.contains(&"files/src/lib.rs.md".to_string()));
        assert!(first_written.contains(&"files/src/nested/api.rs.md".to_string()));

        let unchanged_file_doc = out_dir.join("files/src/nested/api.rs.md");
        let mut unchanged_content =
            std::fs::read_to_string(&unchanged_file_doc).expect("unchanged doc content");
        unchanged_content.push_str("\n<!-- preserve unchanged doc -->\n");
        std::fs::write(&unchanged_file_doc, unchanged_content).expect("write unchanged marker");

        std::fs::write(
            project.path().join("src/lib.rs"),
            "pub struct Client;\npub fn connect() {}\n",
        )
        .expect("modify lib");
        let changed_docs = generate_hierarchical_docs(&input, None);
        let changed_written = write_incremental_doc_set(project.path(), &out_dir, &changed_docs)
            .expect("incremental write");
        let unchanged_after =
            std::fs::read_to_string(&unchanged_file_doc).expect("unchanged doc after content");

        assert!(unchanged_after.contains("preserve unchanged doc"));
        assert_eq!(
            changed_written,
            vec![
                "repo.md".to_string(),
                "modules/src.md".to_string(),
                "files/src/lib.rs.md".to_string()
            ]
        );
        let meta =
            std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta log");
        let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta log");
        let generated_docs = meta["generated_docs"].as_array().expect("generated docs");
        assert_eq!(
            generated_docs,
            &vec![
                serde_json::Value::String("repo.md".to_string()),
                serde_json::Value::String("modules/src.md".to_string()),
                serde_json::Value::String("files/src/lib.rs.md".to_string())
            ]
        );

        let reduced_input = CodewikiInput {
            files: vec!["src/lib.rs".to_string()],
            graph_edges: Vec::new(),
            graph_availability: CodewikiGraphAvailability::Available,
            symbols: vec![test_symbol(
                "src/lib.rs",
                "Client",
                "class",
                1,
                "pub struct Client;",
            )],
        };
        let reduced_docs = generate_hierarchical_docs(&reduced_input, None);
        write_incremental_doc_set(project.path(), &out_dir, &reduced_docs)
            .expect("stale docs removed");

        assert!(!unchanged_file_doc.exists());
        let meta =
            std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read final meta");
        let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse final meta");
        assert!(meta["docs"].get("files/src/nested/api.rs.md").is_none());
    }

    #[test]
    fn run_summary_serializes_daemon_contract_keys() {
        let summary = CodewikiRunSummary {
            command: "codewiki",
            project_id: "project-1".to_string(),
            project_root: "/repo".to_string(),
            out_dir: "/repo/codewiki".to_string(),
            generated_pages: 3,
            changed_paths: vec!["repo.md".to_string()],
            skipped: 2,
            files: 1,
            modules: 1,
            symbols: 4,
            ai_enabled: false,
        };

        let value = serde_json::to_value(summary).expect("summary json");

        assert_eq!(value["command"], "codewiki");
        assert_eq!(value["project_id"], "project-1");
        assert_eq!(value["project_root"], "/repo");
        assert_eq!(value["changed_paths"][0], "repo.md");
        assert_eq!(value["skipped"], 2);
        assert_eq!(value["ai_enabled"], false);
    }

    #[test]
    fn component_id_uses_stored_symbol_id() {
        let mut symbol = test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;");
        symbol.id = "stored-symbol-id".to_string();
        assert_eq!(component_id(&symbol), "stored-symbol-id");
    }

    #[test]
    #[cfg(unix)]
    fn write_doc_rejects_symlinked_parent() {
        use std::os::unix::fs::symlink;

        let project = tempfile::tempdir().expect("project tempdir");
        let out_dir = project.path().join("codewiki");
        let outside = tempfile::tempdir().expect("outside tempdir");
        std::fs::create_dir_all(&out_dir).expect("out dir");
        symlink(outside.path(), out_dir.join("linked")).expect("symlink parent");

        let err = write_doc(&out_dir, "linked/escape.md", "escaped")
            .expect_err("symlink parent should be rejected");

        assert!(err.to_string().contains("symlinked codewiki path"));
        assert!(!outside.path().join("escape.md").exists());
    }

    #[test]
    #[cfg(unix)]
    fn write_doc_rejects_symlinked_target() {
        use std::os::unix::fs::symlink;

        let project = tempfile::tempdir().expect("project tempdir");
        let out_dir = project.path().join("codewiki");
        let outside = tempfile::tempdir().expect("outside tempdir");
        std::fs::create_dir_all(&out_dir).expect("out dir");
        let outside_target = outside.path().join("target.md");
        symlink(&outside_target, out_dir.join("target.md")).expect("symlink target");

        let err = write_doc(&out_dir, "target.md", "escaped").expect_err("symlink target rejected");

        assert!(err.to_string().contains("symlinked codewiki path"));
        assert!(!outside_target.exists());
    }

    fn test_symbol(
        file_path: &str,
        name: &str,
        kind: &str,
        line_start: usize,
        signature: &str,
    ) -> Symbol {
        test_symbol_with_qualified(file_path, name, name, kind, line_start, signature)
    }

    fn test_component_id(file_path: &str, name: &str, kind: &str) -> String {
        Symbol::make_id("project-1", file_path, name, kind, 0)
    }

    fn test_symbol_with_qualified(
        file_path: &str,
        name: &str,
        qualified_name: &str,
        kind: &str,
        line_start: usize,
        signature: &str,
    ) -> Symbol {
        Symbol {
            id: Symbol::make_id("project-1", file_path, name, kind, 0),
            project_id: "project-1".to_string(),
            file_path: file_path.to_string(),
            name: name.to_string(),
            qualified_name: qualified_name.to_string(),
            kind: kind.to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 0,
            line_start,
            line_end: line_start,
            signature: Some(signature.to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    fn test_symbol_range(
        file_path: &str,
        name: &str,
        kind: &str,
        line_start: usize,
        line_end: usize,
        signature: &str,
    ) -> Symbol {
        Symbol {
            id: Symbol::make_id("project-1", file_path, name, kind, 0),
            project_id: "project-1".to_string(),
            file_path: file_path.to_string(),
            name: name.to_string(),
            qualified_name: name.to_string(),
            kind: kind.to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 0,
            line_start,
            line_end,
            signature: Some(signature.to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }
}
