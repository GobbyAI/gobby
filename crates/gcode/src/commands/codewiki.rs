use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use gobby_core::ai::{daemon::generate_via_daemon, effective_route, text::generate_text};
use gobby_core::ai_context::{AiConfigSource, AiContext, PostgresAiConfigSource};
use gobby_core::config::{AiCapability, AiRouting};
use serde::Serialize;

use crate::commands::scope;
use crate::config::{self, Context};
use crate::db;
use crate::falkor;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::secrets;
use crate::visibility;

const DEFAULT_OUT_DIR: &str = "codewiki";

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
                    "- {} [{}] component {} lines {}-{}: {}",
                    symbol.name,
                    symbol.kind,
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
    pub symbols: Vec<Symbol>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodewikiGraphEdge {
    pub source_component_id: String,
    pub target_component_id: String,
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
    pub out_dir: String,
    pub files: usize,
    pub modules: usize,
    pub symbols: usize,
    pub ai_enabled: bool,
}

pub type TextGenerator<'a> = dyn FnMut(&str, &str) -> Option<String> + 'a;

pub fn run(
    ctx: &Context,
    out: Option<String>,
    scope_args: Vec<String>,
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

    let graph_edges = fetch_codewiki_graph_edges(ctx, &files, &symbols)?;
    let input = CodewikiInput {
        files,
        graph_edges,
        symbols,
    };
    let mut generator = resolve_text_generator(ctx);
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
    write_doc_set(Path::new(&out_dir), &docs)?;

    let summary = CodewikiRunSummary {
        out_dir,
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
    let module_docs = build_module_docs(&file_docs, &mut generate);
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
        let target = safe_doc_path(out_dir, relative_path)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(target, content)?;
    }
    Ok(())
}

fn fetch_codewiki_graph_edges(
    ctx: &Context,
    files: &[String],
    symbols: &[Symbol],
) -> anyhow::Result<Vec<CodewikiGraphEdge>> {
    let symbol_components = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| (symbol.id.clone(), component_id(symbol)))
        .collect::<HashMap<_, _>>();
    if symbol_components.is_empty() {
        return Ok(Vec::new());
    }

    let symbol_ids = symbol_components.keys().cloned().collect::<Vec<_>>();
    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();
    falkor::with_falkor(ctx, Vec::new(), |client| {
        let mut edges = Vec::new();
        let (query, params) = codewiki_call_edges_query(&ctx.project_id, &symbol_ids);
        for row in client.query(&query, Some(params))? {
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
            edges.push(CodewikiGraphEdge {
                source_component_id,
                target_component_id,
            });
        }

        if !core_files.is_empty() {
            let file_symbols = symbols_by_file_component(symbols);
            let (query, params) = codewiki_import_edges_query(&ctx.project_id, &core_files);
            for row in client.query(&query, Some(params))? {
                let Some(source_file) = row.get("source").and_then(|value| value.as_str()) else {
                    continue;
                };
                let Some(target_module) = row.get("target").and_then(|value| value.as_str()) else {
                    continue;
                };
                let Some(source_component_id) =
                    first_component_for_file(&file_symbols, source_file)
                else {
                    continue;
                };
                for target_file in files_for_import_target(&core_files, target_module) {
                    let Some(target_component_id) =
                        first_component_for_file(&file_symbols, target_file)
                    else {
                        continue;
                    };
                    edges.push(CodewikiGraphEdge {
                        source_component_id: source_component_id.clone(),
                        target_component_id,
                    });
                }
            }
        }

        Ok(edges)
    })
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
    for edge in graph_edges {
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
    let parent = parents
        .get(file)
        .cloned()
        .unwrap_or_else(|| file.to_string());
    if parent == file {
        return parent;
    }
    let root = find_file_root(parents, &parent);
    parents.insert(file.to_string(), root.clone());
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
            .flat_map(|file| file.component_ids.iter().cloned())
            .collect::<Vec<_>>();
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
        });
    }

    docs.sort_by(|a, b| a.module.cmp(&b.module));
    docs
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
            let _ = writeln!(doc, "- `{}`", inline_code(component_id));
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
            "- `{}` ({}) component `{}` lines {}-{} {}",
            inline_code(&symbol.symbol.qualified_name),
            symbol.symbol.kind,
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
            let _ = writeln!(doc, "  - Signature: `{}`", inline_code(signature));
        }
        let _ = writeln!(doc, "  - Purpose: {}", symbol.purpose);
    }
    doc.push('\n');
    doc
}

fn resolve_text_generator(ctx: &Context) -> Option<Box<TextGenerator<'static>>> {
    let ai_context = resolve_ai_context(ctx).ok()?;
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

fn resolve_ai_context(ctx: &Context) -> anyhow::Result<AiContext> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    Ok(AiContext::resolve(
        Some(ctx.project_id.clone()),
        &mut source,
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
    value.replace('`', "\\`").replace('\n', " ")
}

fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
}

fn component_id(symbol: &Symbol) -> String {
    format!("{}::{}", symbol.file_path, symbol.name)
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
    fn clusters_modules_from_graph() {
        let input = CodewikiInput {
            files: vec![
                "src/api/handler.rs".to_string(),
                "src/domain/service.rs".to_string(),
                "tests/domain/service_test.rs".to_string(),
                "vendor/generated/client.rs".to_string(),
            ],
            graph_edges: vec![CodewikiGraphEdge {
                source_component_id: "src/api/handler.rs::handle".to_string(),
                target_component_id: "src/domain/service.rs::Service".to_string(),
            }],
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
        assert!(module.contains("src/api/handler.rs::handle"));
        assert!(module.contains("src/domain/service.rs::Service"));
        assert!(!docs_by_path.contains_key("files/tests/domain/service_test.rs.md"));
        assert!(!docs_by_path.contains_key("files/vendor/generated/client.rs.md"));
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
                .contains("src/api/handler.rs::handle")
        );
        assert!(
            docs_by_path
                .get("files/src/domain/service.rs.md")
                .expect("service file doc")
                .contains("src/domain/service.rs::Service")
        );
        assert!(
            docs_by_path
                .get("files/src/domain/service.rs.md")
                .expect("service file doc")
                .contains("src/domain/service.rs::new")
        );
        assert!(
            !docs_by_path
                .get("files/src/domain/service.rs.md")
                .expect("service file doc")
                .contains("src/domain/service.rs::Service::new")
        );
    }

    #[test]
    fn citations_validated_against_spans() {
        let input = CodewikiInput {
            files: vec!["src/lib.rs".to_string()],
            graph_edges: Vec::new(),
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

    fn test_symbol(
        file_path: &str,
        name: &str,
        kind: &str,
        line_start: usize,
        signature: &str,
    ) -> Symbol {
        test_symbol_with_qualified(file_path, name, name, kind, line_start, signature)
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
            id: format!("{file_path}:{name}"),
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
            id: format!("{file_path}:{name}"),
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