//! Tree-sitter AST parsing for symbol, import, and call extraction.
//! Ports logic from src/gobby/code_index/parser.py.

use std::collections::HashSet;
use std::path::Path;

use anyhow::Context as _;
use streaming_iterator::StreamingIterator;
use tree_sitter::{Parser, Query, QueryCursor};

mod calls;

use crate::index::hasher::symbol_content_hash;
use crate::index::import_resolution::{self, ExtractedImports};
use crate::index::languages;
use crate::index::security;
use crate::index::semantic::SemanticCallResolver;
use crate::models::{ParseResult, Symbol};
use calls::{CallExtractionContext, extract_calls};

pub use crate::index::import_resolution::{
    ImportResolutionContext, build_import_resolution_context,
};

#[cfg(test)]
use calls::{call_qualifier_path, line_terminator_len, split_qualified_callee};

/// Maximum file size to index (10 MB).
const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;

pub(crate) fn parse_file_with_semantic(
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[impl AsRef<str>],
    import_context: &ImportResolutionContext,
    semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Option<ParseResult>> {
    // Security checks
    if !security::validate_path(file_path, root_path) {
        return Ok(None);
    }
    if !security::is_symlink_safe(file_path, root_path) {
        return Ok(None);
    }
    if security::should_exclude_path(root_path, file_path, exclude_patterns) {
        return Ok(None);
    }
    if security::has_secret_extension(file_path) {
        return Ok(None);
    }

    let Ok(meta) = file_path.metadata() else {
        return Ok(None);
    };
    if meta.len() == 0 || meta.len() > MAX_FILE_SIZE {
        return Ok(None);
    }

    if security::is_binary(file_path) {
        return Ok(None);
    }

    let file_str = file_path.to_string_lossy();
    let Some(language) = languages::detect_language(&file_str) else {
        return Ok(None);
    };
    let Some(spec) = languages::get_spec(language) else {
        return Ok(None);
    };
    let Some(ts_lang) = languages::get_ts_language_for_path(language, &file_str) else {
        return Ok(None);
    };

    let Ok(source) = std::fs::read(file_path) else {
        return Ok(None);
    };

    let mut parser = Parser::new();
    if parser.set_language(&ts_lang).is_err() {
        return Ok(None);
    }
    let Some(tree) = parser.parse(&source, None) else {
        return Ok(None);
    };

    let rel_path = file_path
        .canonicalize()
        .ok()
        .and_then(|abs| {
            root_path.canonicalize().ok().and_then(|root| {
                abs.strip_prefix(&root)
                    .ok()
                    .map(|p| p.to_string_lossy().to_string())
            })
        })
        .unwrap_or_else(|| file_str.to_string());

    let mut symbols = extract_symbols(
        &tree, &source, spec, language, &ts_lang, project_id, &rel_path,
    )?;
    link_parents(&mut symbols);
    let extracted_imports = extract_imports(
        &tree,
        &source,
        spec,
        language,
        &ts_lang,
        &rel_path,
        import_context,
    )?;
    let calls = extract_calls(
        &tree,
        &source,
        spec,
        CallExtractionContext {
            language,
            ts_lang: &ts_lang,
            rel_path: &rel_path,
            symbols: &symbols,
            import_context,
            import_bindings: &extracted_imports.bindings,
            file_path,
            root_path,
        },
        semantic_resolver,
    )?;

    Ok(Some(ParseResult {
        symbols,
        imports: extracted_imports.imports,
        calls,
        source,
    }))
}

fn extract_symbols(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    language: &str,
    ts_lang: &tree_sitter::Language,
    project_id: &str,
    rel_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    if spec.symbol_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = Query::new(ts_lang, spec.symbol_query).with_context(|| {
        format!("failed to compile symbol query for language `{language}` while parsing {rel_path}")
    })?;

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);

    let mut symbols = Vec::new();
    let mut seen_ids = HashSet::new();
    let capture_names = query.capture_names();
    let name_capture = capture_names.iter().position(|name| *name == "name");
    let definition_kinds = capture_names
        .iter()
        .map(|name| name.strip_prefix("definition."))
        .collect::<Vec<_>>();

    while let Some(m) = matches.next() {
        let mut name_text: Option<String> = None;
        let mut def_node = None;
        let mut kind = String::from("function");

        for cap in m.captures {
            let capture_index = cap.index as usize;
            if name_capture == Some(capture_index) {
                name_text = Some(
                    String::from_utf8_lossy(&source[cap.node.start_byte()..cap.node.end_byte()])
                        .to_string(),
                );
            } else if let Some(Some(k)) = definition_kinds.get(capture_index) {
                def_node = Some(cap.node);
                kind = (*k).to_string();
            }
        }

        let (name, node) = match (name_text, def_node) {
            (Some(n), Some(d)) => (n, d),
            _ => continue,
        };

        // Signature: first line of definition
        let sig_end = source[node.start_byte()..]
            .iter()
            .position(|&b| b == b'\n')
            .map(|p| node.start_byte() + p)
            .unwrap_or(node.end_byte());
        let mut signature = String::from_utf8_lossy(&source[node.start_byte()..sig_end])
            .trim()
            .to_string();
        if signature.len() > 200 {
            signature.truncate(200);
            signature.push_str("...");
        }

        let docstring = extract_docstring(&node, source, language);
        let c_hash =
            symbol_content_hash(source, node.start_byte(), node.end_byte()).unwrap_or_default();
        let symbol_id = Symbol::make_id(project_id, rel_path, &name, &kind, node.start_byte());

        if seen_ids.contains(&symbol_id) {
            continue;
        }
        seen_ids.insert(symbol_id.clone());

        symbols.push(Symbol {
            id: symbol_id,
            project_id: project_id.to_string(),
            file_path: rel_path.to_string(),
            name: name.clone(),
            qualified_name: name,
            kind,
            language: language.to_string(),
            byte_start: node.start_byte(),
            byte_end: node.end_byte(),
            line_start: node.start_position().row + 1,
            line_end: node.end_position().row + 1,
            signature: Some(signature),
            docstring,
            parent_symbol_id: None,
            content_hash: c_hash,
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        });
    }

    Ok(symbols)
}

fn link_parents(symbols: &mut [Symbol]) {
    let mut indices: Vec<usize> = (0..symbols.len()).collect();
    indices.sort_by_key(|&i| symbols[i].byte_start);

    for idx in 0..indices.len() {
        let i = indices[idx];
        for jdx in (0..idx).rev() {
            let j = indices[jdx];
            let parent_kind = symbols[j].kind.as_str();
            if (parent_kind == "class" || parent_kind == "type")
                && symbols[j].byte_start <= symbols[i].byte_start
                && symbols[j].byte_end >= symbols[i].byte_end
            {
                let parent_name = symbols[j].name.clone();
                let parent_id = symbols[j].id.clone();
                let sym = &mut symbols[i];
                sym.parent_symbol_id = Some(parent_id);
                sym.qualified_name = format!("{}.{}", parent_name, sym.name);
                if sym.kind == "function" {
                    sym.kind = "method".to_string();
                }
                break;
            }
        }
    }
}

fn extract_docstring(node: &tree_sitter::Node, source: &[u8], language: &str) -> Option<String> {
    if !matches!(language, "python" | "javascript" | "typescript") {
        return None;
    }

    let mut body = None;
    let mut walk = node.walk();
    for child in node.children(&mut walk) {
        let ty = child.kind();
        if ty == "block" || ty == "statement_block" {
            body = Some(child);
            break;
        }
    }
    let body = body?;

    let mut walk2 = body.walk();
    for child in body.children(&mut walk2) {
        let ty = child.kind();
        if ty == "comment" || ty == "\n" || ty == "newline" {
            continue;
        }

        let string_node = if ty == "string" {
            Some(child)
        } else if ty == "expression_statement" {
            let mut w3 = child.walk();
            child.children(&mut w3).find(|gc| gc.kind() == "string")
        } else {
            None
        };

        let string_node = string_node?;

        // Try string_content child first
        let mut w4 = string_node.walk();
        for sc in string_node.children(&mut w4) {
            if sc.kind() == "string_content" {
                let raw = String::from_utf8_lossy(&source[sc.start_byte()..sc.end_byte()]);
                let trimmed = raw.trim();
                return if trimmed.is_empty() {
                    None
                } else {
                    Some(trimmed.to_string())
                };
            }
        }

        // Fallback: strip quotes
        let raw =
            String::from_utf8_lossy(&source[string_node.start_byte()..string_node.end_byte()]);
        let raw = raw.trim();
        let stripped = strip_quotes(raw);
        return if stripped.is_empty() {
            None
        } else {
            Some(stripped.to_string())
        };
    }

    None
}

fn strip_quotes(s: &str) -> &str {
    for q in &["\"\"\"", "'''", "\"", "'"] {
        if s.starts_with(q) && s.ends_with(q) && s.len() >= q.len() * 2 {
            return s[q.len()..s.len() - q.len()].trim();
        }
    }
    s
}

fn extract_imports(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    language: &str,
    ts_lang: &tree_sitter::Language,
    rel_path: &str,
    import_context: &ImportResolutionContext,
) -> anyhow::Result<ExtractedImports> {
    if spec.import_query.trim().is_empty() {
        return Ok(ExtractedImports::default());
    }

    let query = Query::new(ts_lang, spec.import_query).with_context(|| {
        format!("failed to compile import query for language `{language}` while parsing {rel_path}")
    })?;

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names = query.capture_names();
    let import_capture = capture_names.iter().position(|name| *name == "import");
    let mut extracted = ExtractedImports::default();

    while let Some(m) = matches.next() {
        for cap in m.captures {
            if import_capture == Some(cap.index as usize) {
                let text =
                    String::from_utf8_lossy(&source[cap.node.start_byte()..cap.node.end_byte()])
                        .trim()
                        .to_string();
                import_resolution::parse_import_statement(
                    language,
                    &text,
                    rel_path,
                    import_context,
                    &mut extracted,
                )?;
            }
        }
    }

    import_resolution::seed_import_bindings(language, import_context, &mut extracted.bindings);
    Ok(extracted)
}

#[cfg(test)]
mod tests;
