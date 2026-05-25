//! Tree-sitter AST parsing for symbol, import, and call extraction.
//! Ports logic from src/gobby/code_index/parser.py.

use std::collections::HashSet;
use std::path::Path;

use streaming_iterator::StreamingIterator;
use tree_sitter::{Parser, Query, QueryCursor};

use crate::index::hasher::symbol_content_hash;
use crate::index::import_resolution::{self, ExtractedImports, ImportBindings};
use crate::index::languages;
use crate::index::security;
use crate::index::semantic::{SemanticCallRequest, SemanticCallResolver};
use crate::models::{CallRelation, ParseResult, Symbol};

pub use crate::index::import_resolution::{
    ImportResolutionContext, build_import_resolution_context,
};

/// Maximum file size to index (10 MB).
const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CallSyntaxKind {
    Bare,
    Member,
    Other,
}

pub(crate) fn parse_file_with_semantic(
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
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
    if security::should_exclude(file_path, exclude_patterns) {
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
    let Some(ts_lang) = languages::get_ts_language(language) else {
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
    );
    link_parents(&mut symbols);
    let extracted_imports = extract_imports(
        &tree,
        &source,
        spec,
        language,
        &ts_lang,
        &rel_path,
        import_context,
    );
    let calls = extract_calls(
        &tree,
        &source,
        spec,
        CallExtractionContext {
            language,
            ts_lang: &ts_lang,
            rel_path: &rel_path,
            symbols: &symbols,
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
) -> Vec<Symbol> {
    if spec.symbol_query.trim().is_empty() {
        return Vec::new();
    }

    let query = match Query::new(ts_lang, spec.symbol_query) {
        Ok(q) => q,
        Err(_) => return Vec::new(),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);

    let mut symbols = Vec::new();
    let mut seen_ids = HashSet::new();
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();

    while let Some(m) = matches.next() {
        let mut name_text: Option<String> = None;
        let mut def_node = None;
        let mut kind = String::from("function");

        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "name" {
                name_text = Some(
                    String::from_utf8_lossy(&source[cap.node.start_byte()..cap.node.end_byte()])
                        .to_string(),
                );
            } else if let Some(k) = cap_name.strip_prefix("definition.") {
                def_node = Some(cap.node);
                kind = k.to_string();
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

    symbols
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
) -> ExtractedImports {
    if spec.import_query.trim().is_empty() {
        return ExtractedImports::default();
    }

    let query = match Query::new(ts_lang, spec.import_query) {
        Ok(q) => q,
        Err(_) => return ExtractedImports::default(),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let mut extracted = ExtractedImports::default();

    while let Some(m) = matches.next() {
        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "import" {
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
                );
            }
        }
    }

    import_resolution::seed_import_bindings(language, import_context, &mut extracted.bindings);
    extracted
}

struct CallExtractionContext<'a> {
    language: &'a str,
    ts_lang: &'a tree_sitter::Language,
    rel_path: &'a str,
    symbols: &'a [Symbol],
    import_bindings: &'a ImportBindings,
    file_path: &'a Path,
    root_path: &'a Path,
}

fn extract_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let language = ctx.language;
    let rel_path = ctx.rel_path;
    let symbols = ctx.symbols;
    let import_bindings = ctx.import_bindings;
    if language == "dart" {
        return extract_textual_dart_calls(
            source,
            rel_path,
            symbols,
            import_bindings,
            ctx.file_path,
            ctx.root_path,
            semantic_resolver,
        );
    }
    if spec.call_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = match Query::new(ctx.ts_lang, spec.call_query) {
        Ok(q) => q,
        Err(_) => return Ok(Vec::new()),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let mut calls = Vec::new();

    while let Some(m) = matches.next() {
        let mut name_node = None;
        let mut call_node = None;

        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "name" {
                name_node = Some(cap.node);
            } else if cap_name == "call" {
                call_node = Some(cap.node);
            }
        }

        let name_n = match name_node {
            Some(n) => n,
            None => continue,
        };

        let raw_callee =
            String::from_utf8_lossy(&source[name_n.start_byte()..name_n.end_byte()]).to_string();
        let (callee_name, qualifier_from_name) = split_qualified_callee(&raw_callee);
        if should_ignore_call_name(language, &callee_name) {
            continue;
        }

        let target = call_node.unwrap_or(name_n);
        let caller_symbol = enclosing_symbol(symbols, target.start_byte());
        let caller_symbol_id = caller_symbol.map(|s| s.id.clone()).unwrap_or_default();
        // If the captured callee is already qualified, trust that text over a
        // prefix inferred from the wider call node.
        let qualifier_path = call_qualifier_path(qualifier_from_name, || {
            member_qualifier_path(source, target, name_n)
        });
        let detected_syntax = call_syntax_kind(name_n, target);
        let syntax = if detected_syntax == CallSyntaxKind::Bare && qualifier_path.is_some() {
            CallSyntaxKind::Member
        } else {
            detected_syntax
        };
        let local_target = resolve_same_file_callee_for_language(
            language,
            symbols,
            caller_symbol,
            &callee_name,
            syntax,
        );
        let root_alias = qualifier_path
            .as_deref()
            .and_then(qualifier_root_alias)
            .map(ToOwned::to_owned);
        let external_target = import_resolution::resolve_external_callee(
            import_bindings,
            symbols,
            &callee_name,
            root_alias.as_deref(),
            qualifier_path.as_deref(),
            syntax == CallSyntaxKind::Bare,
        );
        let semantic_target = if local_target.is_none() && external_target.is_none() {
            if let Some(resolver) = semantic_resolver.as_deref_mut() {
                resolver.resolve(&SemanticCallRequest {
                    language,
                    file_path: ctx.file_path,
                    root_path: ctx.root_path,
                    source,
                    callee_name: &callee_name,
                    line: name_n.start_position().row + 1,
                    column: utf16_column_at_byte(source, name_n.start_byte()),
                })?
            } else {
                None
            }
        } else {
            None
        };

        let mut call = CallRelation::new(
            caller_symbol_id,
            callee_name,
            rel_path.to_string(),
            name_n.start_position().row + 1,
        );
        match (local_target, external_target) {
            (Some(callee_symbol_id), None) => {
                call = call.with_symbol_target(callee_symbol_id);
            }
            (None, Some(external_target)) => {
                call =
                    call.with_external_target(external_target.callee_name, external_target.module);
            }
            (None, None) => {
                if let Some(semantic_target) = semantic_target {
                    call = call.with_external_target(
                        semantic_target.callee_name,
                        semantic_target.external_module,
                    );
                }
            }
            _ => {}
        }
        calls.push(call);
    }

    Ok(calls)
}

fn extract_textual_dart_calls(
    source: &[u8],
    rel_path: &str,
    symbols: &[Symbol],
    import_bindings: &ImportBindings,
    file_path: &Path,
    root_path: &Path,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let text = String::from_utf8_lossy(source);
    let mut calls = Vec::new();
    let mut line_start_byte = 0usize;

    for (row, line) in text.lines().enumerate() {
        let terminator_len = line_terminator_len(&text, line_start_byte, line.len());
        let trimmed = line.trim_start();
        if trimmed.starts_with("import ")
            || trimmed.starts_with("export ")
            || trimmed.starts_with("class ")
            || trimmed.starts_with("enum ")
            || trimmed.starts_with("typedef ")
        {
            line_start_byte += line.len() + terminator_len;
            continue;
        }

        for candidate in textual_call_candidates(line, line_start_byte, &['.']) {
            if should_ignore_call_name("dart", &candidate.name) {
                continue;
            }
            let caller_symbol = enclosing_symbol(symbols, candidate.call_byte);
            let caller_symbol_id = caller_symbol.map(|s| s.id.clone()).unwrap_or_default();
            let syntax = if candidate.qualifier_path.is_some() {
                CallSyntaxKind::Member
            } else {
                CallSyntaxKind::Bare
            };
            let local_target = resolve_same_file_callee_for_language(
                "dart",
                symbols,
                caller_symbol,
                &candidate.name,
                syntax,
            );
            let root_alias = candidate
                .qualifier_path
                .as_deref()
                .and_then(qualifier_root_alias)
                .map(ToOwned::to_owned);
            let external_target = import_resolution::resolve_external_callee(
                import_bindings,
                symbols,
                &candidate.name,
                root_alias.as_deref(),
                candidate.qualifier_path.as_deref(),
                syntax == CallSyntaxKind::Bare,
            );
            let semantic_target = if local_target.is_none() && external_target.is_none() {
                if let Some(resolver) = semantic_resolver.as_deref_mut() {
                    resolver.resolve(&SemanticCallRequest {
                        language: "dart",
                        file_path,
                        root_path,
                        source,
                        callee_name: &candidate.name,
                        line: row + 1,
                        column: utf16_column_at_byte(source, candidate.call_byte),
                    })?
                } else {
                    None
                }
            } else {
                None
            };

            let mut call = CallRelation::new(
                caller_symbol_id,
                candidate.name,
                rel_path.to_string(),
                row + 1,
            );
            match (local_target, external_target) {
                (Some(callee_symbol_id), None) => {
                    call = call.with_symbol_target(callee_symbol_id);
                }
                (None, Some(external_target)) => {
                    call = call
                        .with_external_target(external_target.callee_name, external_target.module);
                }
                (None, None) => {
                    if let Some(semantic_target) = semantic_target {
                        call = call.with_external_target(
                            semantic_target.callee_name,
                            semantic_target.external_module,
                        );
                    }
                }
                _ => {}
            }
            calls.push(call);
        }

        line_start_byte += line.len() + terminator_len;
    }

    Ok(calls)
}

fn line_terminator_len(text: &str, line_start_byte: usize, line_len: usize) -> usize {
    let terminator_start = line_start_byte + line_len;
    let Some(rest) = text.as_bytes().get(terminator_start..) else {
        return 0;
    };
    if rest.starts_with(b"\r\n") {
        2
    } else if rest.starts_with(b"\n") {
        1
    } else {
        0
    }
}

fn utf16_column_at_byte(source: &[u8], byte_offset: usize) -> usize {
    let byte_offset = byte_offset.min(source.len());
    let line_start = source[..byte_offset]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map(|idx| idx + 1)
        .unwrap_or(0);
    String::from_utf8_lossy(&source[line_start..byte_offset])
        .encode_utf16()
        .count()
}

#[derive(Debug)]
struct TextualCallCandidate {
    name: String,
    qualifier_path: Option<String>,
    call_byte: usize,
}

fn textual_call_candidates(
    line: &str,
    line_start_byte: usize,
    separators: &[char],
) -> Vec<TextualCallCandidate> {
    let bytes = line.as_bytes();
    let mut candidates = Vec::new();
    let mut idx = 0usize;

    while idx < bytes.len() {
        if bytes[idx] != b'(' {
            idx += 1;
            continue;
        }
        let mut end = idx;
        while end > 0 && bytes[end - 1].is_ascii_whitespace() {
            end -= 1;
        }
        let mut start = end;
        while start > 0 && is_textual_call_name_byte(bytes[start - 1]) {
            start -= 1;
        }
        if start == end {
            idx += 1;
            continue;
        }

        let name = &line[start..end];
        if looks_like_textual_function_declaration(line, start, idx) {
            idx += 1;
            continue;
        }
        let mut qualifier_path = None;
        let mut prefix_end = start;
        while prefix_end > 0 && bytes[prefix_end - 1].is_ascii_whitespace() {
            prefix_end -= 1;
        }
        if prefix_end > 0 && separators.contains(&(bytes[prefix_end - 1] as char)) {
            let mut qualifier_start = prefix_end - 1;
            while qualifier_start > 0 {
                let ch = bytes[qualifier_start - 1] as char;
                if is_identifier_continue(ch) || separators.contains(&ch) {
                    qualifier_start -= 1;
                } else {
                    break;
                }
            }
            let qualifier = line[qualifier_start..prefix_end - 1].trim();
            if !qualifier.is_empty() {
                qualifier_path = Some(qualifier.to_string());
            }
        }

        candidates.push(TextualCallCandidate {
            name: name.to_string(),
            qualifier_path,
            call_byte: line_start_byte + start,
        });
        idx += 1;
    }

    candidates
}

fn looks_like_textual_function_declaration(
    line: &str,
    name_start: usize,
    open_paren: usize,
) -> bool {
    let prefix = line[..name_start].trim_end();
    let after_paren = &line[open_paren + 1..];
    let after_args = after_paren
        .find(')')
        .and_then(|close| after_paren.get(close + 1..))
        .unwrap_or_default()
        .trim_start();
    let has_declaration_tail = after_args.starts_with('{')
        || after_args.starts_with("=>")
        || after_args.starts_with("async")
        || after_args.starts_with("sync")
        || after_args.starts_with("external")
        || after_args.starts_with(';');
    if !has_declaration_tail {
        return false;
    }

    if prefix.is_empty() {
        return !after_args.starts_with(';');
    }
    if prefix.contains(['=', '.', '(', ',', ';']) {
        return false;
    }
    let Some(previous_token) = prefix.split_whitespace().last() else {
        return false;
    };
    previous_token.contains('<')
        || previous_token
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_uppercase())
        || matches!(
            previous_token,
            "void"
                | "int"
                | "double"
                | "num"
                | "String"
                | "bool"
                | "dynamic"
                | "Object"
                | "Future"
                | "Stream"
        )
}

fn enclosing_symbol(symbols: &[Symbol], byte_offset: usize) -> Option<&Symbol> {
    symbols
        .iter()
        .rfind(|s| s.byte_start <= byte_offset && byte_offset <= s.byte_end)
}

fn call_syntax_kind(name_node: tree_sitter::Node, call_node: tree_sitter::Node) -> CallSyntaxKind {
    let Some(mut ancestor) = name_node.parent() else {
        return CallSyntaxKind::Other;
    };
    if ancestor.id() == call_node.id() {
        return CallSyntaxKind::Bare;
    }

    loop {
        if is_memberish_kind(ancestor.kind()) {
            return CallSyntaxKind::Member;
        }
        if ancestor.id() == call_node.id() {
            return CallSyntaxKind::Other;
        }
        let Some(parent) = ancestor.parent() else {
            return CallSyntaxKind::Other;
        };
        ancestor = parent;
    }
}

fn is_memberish_kind(kind: &str) -> bool {
    matches!(
        kind,
        "attribute"
            | "member_expression"
            | "selector_expression"
            | "field_expression"
            | "member_access_expression"
            | "member_call_expression"
            | "navigation_expression"
            | "scoped_call_expression"
            | "dot"
    )
}

fn is_callable_kind(kind: &str) -> bool {
    matches!(kind, "function" | "method")
}

fn resolve_same_file_callee(
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    syntax: CallSyntaxKind,
) -> Option<String> {
    match syntax {
        CallSyntaxKind::Bare => unique_symbol_id(
            symbols
                .iter()
                .filter(|symbol| symbol.name == callee_name && is_callable_kind(&symbol.kind)),
        ),
        CallSyntaxKind::Member => {
            let parent_symbol_id =
                caller_symbol.and_then(|symbol| symbol.parent_symbol_id.as_deref())?;
            unique_symbol_id(symbols.iter().filter(|symbol| {
                symbol.name == callee_name
                    && symbol.kind == "method"
                    && symbol.parent_symbol_id.as_deref() == Some(parent_symbol_id)
            }))
        }
        CallSyntaxKind::Other => None,
    }
}

fn resolve_same_file_callee_for_language(
    language: &str,
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    syntax: CallSyntaxKind,
) -> Option<String> {
    if language == "ruby" && syntax == CallSyntaxKind::Bare {
        // Ruby bare calls are dynamic dispatch/open-class territory; same-file
        // name matching creates noisy false edges here.
        return None;
    }
    resolve_same_file_callee(symbols, caller_symbol, callee_name, syntax)
}

fn unique_symbol_id<'a>(symbols: impl Iterator<Item = &'a Symbol>) -> Option<String> {
    let mut symbols = symbols;
    let first = symbols.next()?;
    if symbols.next().is_some() {
        None
    } else {
        Some(first.id.clone())
    }
}

fn member_qualifier_path(
    source: &[u8],
    call_node: tree_sitter::Node,
    name_node: tree_sitter::Node,
) -> Option<String> {
    let prefix = String::from_utf8_lossy(&source[call_node.start_byte()..name_node.start_byte()]);
    let prefix = prefix.trim();
    if prefix.starts_with('$') || prefix.contains("->") || prefix.contains("?->") {
        return None;
    }
    let is_absolute_namespace = prefix.starts_with('\\');

    let mut chars = prefix
        .trim_end_matches(|ch: char| ch == '.' || ch == ':' || ch == '\\' || ch.is_whitespace())
        .chars()
        .skip_while(|c| !is_identifier_start(*c));
    let first = chars.next()?;
    if !is_identifier_start(first) {
        return None;
    }

    let mut qualifier = if is_absolute_namespace {
        "\\".to_string()
    } else {
        String::new()
    };
    qualifier.push(first);
    for ch in chars {
        if is_identifier_continue(ch) || matches!(ch, '.' | ':' | '\\') {
            qualifier.push(ch);
        } else {
            break;
        }
    }

    let qualifier = qualifier.trim_end_matches(['.', ':', '\\']).to_string();
    if qualifier.is_empty() {
        None
    } else {
        Some(qualifier)
    }
}

fn call_qualifier_path(
    qualifier_from_name: Option<String>,
    qualifier_from_member: impl FnOnce() -> Option<String>,
) -> Option<String> {
    qualifier_from_name.or_else(qualifier_from_member)
}

fn split_qualified_callee(raw: &str) -> (String, Option<String>) {
    let raw = raw.trim();
    for separator in ["::", "\\", "."] {
        if let Some((qualifier, name)) = raw.rsplit_once(separator)
            && !qualifier.is_empty()
            && !name.is_empty()
        {
            return (name.to_string(), Some(qualifier.to_string()));
        }
    }
    (raw.to_string(), None)
}

fn qualifier_root_alias(qualifier: &str) -> Option<&str> {
    qualifier
        .trim_start_matches('\\')
        .split(['.', ':', '\\'])
        .find(|part| !part.is_empty())
}

fn is_identifier_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || matches!(ch, '_' | '$')
}

fn is_identifier_continue(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '$')
}

fn is_textual_call_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$' | b'!' | b'?')
}

fn should_ignore_call_name(language: &str, name: &str) -> bool {
    match language {
        "dart" => matches!(
            name,
            "if" | "for" | "while" | "switch" | "catch" | "assert" | "return" | "throw"
        ),
        "elixir" => matches!(
            name,
            "def" | "defp" | "defmacro" | "defmodule" | "alias" | "import" | "use" | "require"
        ),
        "kotlin" => matches!(
            name,
            "if" | "for" | "while" | "when" | "catch" | "return" | "throw"
        ),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};

    use tempfile::TempDir;

    use super::*;

    fn parse_source(file_name: &str, source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        for (path, contents) in extra_files {
            let file_path = root.join(path);
            if let Some(parent) = file_path.parent() {
                fs::create_dir_all(parent).expect("create parent dirs");
            }
            fs::write(&file_path, contents).expect("write extra source");
        }

        let path = root.join(file_name);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create parent dirs");
        }
        fs::write(&path, source).expect("write test source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        parse_file_with_semantic(&path, "proj", root, &[], &context, None)
            .expect("parse result")
            .expect("parse file")
    }

    fn parse_python(source: &str) -> ParseResult {
        parse_source("sample.py", source, &[])
    }

    fn parse_javascript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/sample.js", source, extra_files)
    }

    fn parse_typescript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/sample.ts", source, extra_files)
    }

    fn parse_go(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("cmd/sample.go", source, extra_files)
    }

    fn parse_rust(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/main.rs", source, extra_files)
    }

    fn parse_java(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/main/java/app/Sample.java", source, extra_files)
    }

    fn parse_csharp(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/Sample.cs", source, extra_files)
    }

    fn parse_php(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/sample.php", source, extra_files)
    }

    fn parse_ruby(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("lib/sample.rb", source, extra_files)
    }

    fn parse_dart(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("lib/sample.dart", source, extra_files)
    }

    fn parse_elixir(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("lib/sample.ex", source, extra_files)
    }

    fn parse_kotlin(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/main/kotlin/Sample.kt", source, extra_files)
    }

    fn parse_swift(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("Sources/App/main.swift", source, extra_files)
    }

    fn discover_supported_files(root: &Path) -> Vec<PathBuf> {
        let mut candidates = Vec::new();
        let mut stack = vec![root.to_path_buf()];
        while let Some(path) = stack.pop() {
            let entries = fs::read_dir(&path).expect("read dir");
            for entry in entries {
                let entry = entry.expect("dir entry");
                let entry_path = entry.path();
                if entry_path.is_dir() {
                    stack.push(entry_path);
                } else if let Some(language) =
                    languages::detect_language(&entry_path.to_string_lossy())
                    && !language.is_empty()
                {
                    candidates.push(entry_path);
                }
            }
        }
        candidates
    }

    #[test]
    fn line_terminator_len_tracks_lf_crlf_and_eof() {
        let text = "import 'a';\r\nhttp.Client();\nlast()";
        assert_eq!(line_terminator_len(text, 0, "import 'a';".len()), 2);

        let second_start = "import 'a';\r\n".len();
        assert_eq!(
            line_terminator_len(text, second_start, "http.Client();".len()),
            1
        );

        let last_start = "import 'a';\r\nhttp.Client();\n".len();
        assert_eq!(line_terminator_len(text, last_start, "last()".len()), 0);
    }

    struct FakeSemanticResolver {
        target: Option<crate::index::semantic::SemanticCallTarget>,
        expected_language: &'static str,
        expected_callee: &'static str,
        requests: Vec<CapturedSemanticRequest>,
        error: Option<&'static str>,
    }

    struct CapturedSemanticRequest {
        language: String,
        file_path: PathBuf,
        root_path: PathBuf,
        callee_name: String,
        line: usize,
        column: usize,
    }

    impl SemanticCallResolver for FakeSemanticResolver {
        fn resolve(
            &mut self,
            request: &SemanticCallRequest<'_>,
        ) -> anyhow::Result<Option<crate::index::semantic::SemanticCallTarget>> {
            self.requests.push(CapturedSemanticRequest {
                language: request.language.to_string(),
                file_path: request.file_path.to_path_buf(),
                root_path: request.root_path.to_path_buf(),
                callee_name: request.callee_name.to_string(),
                line: request.line,
                column: request.column,
            });
            if let Some(error) = self.error {
                anyhow::bail!("{error}");
            }
            if request.language == self.expected_language
                && request.callee_name == self.expected_callee
            {
                Ok(self.target.clone())
            } else {
                Ok(None)
            }
        }
    }

    #[test]
    fn explicit_qualified_raw_callee_takes_precedence_over_member_prefix() {
        let mut inferred_called = false;
        let (_, qualifier_from_name) = split_qualified_callee("Vendor\\Pkg\\helper");

        let qualifier_path = call_qualifier_path(qualifier_from_name, || {
            inferred_called = true;
            Some("Vendor".to_string())
        });

        assert_eq!(qualifier_path.as_deref(), Some("Vendor\\Pkg"));
        assert!(!inferred_called);
    }

    #[test]
    fn resolves_unique_same_file_bare_calls() {
        let parsed = parse_python(
            r#"
def foo():
    pass

def bar():
    foo()
"#,
        );

        let foo = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "foo")
            .expect("foo symbol");
        let bar = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "bar")
            .expect("bar symbol");
        let call = parsed.calls.first().expect("call");

        assert_eq!(call.caller_symbol_id, bar.id);
        assert_eq!(call.callee_symbol_id.as_deref(), Some(foo.id.as_str()));
    }

    #[test]
    fn resolves_same_class_member_calls() {
        let parsed = parse_python(
            r#"
class Greeter:
    def greet(self):
        self.render()

    def render(self):
        pass
"#,
        );

        let render = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.qualified_name == "Greeter.render")
            .expect("render method");
        let call = parsed.calls.first().expect("call");

        assert_eq!(call.callee_symbol_id.as_deref(), Some(render.id.as_str()));
    }

    #[test]
    fn leaves_ambiguous_bare_calls_unresolved() {
        let parsed = parse_python(
            r#"
def foo():
    pass

class A:
    def foo(self):
        pass

def bar():
    foo()
"#,
        );

        let call = parsed.calls.first().expect("call");
        assert!(call.callee_symbol_id.is_none());
    }

    #[test]
    fn leaves_non_local_member_calls_unresolved() {
        let parsed = parse_python(
            r#"
class A:
    def bar(self):
        obj.render()

class B:
    def render(self):
        pass
"#,
        );

        let call = parsed.calls.first().expect("call");
        assert!(call.callee_symbol_id.is_none());
    }

    #[test]
    fn classifies_external_python_from_import_calls() {
        let parsed = parse_python(
            r#"
from requests import get as fetch

def run():
    fetch()
"#,
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "get");
        assert_eq!(call.callee_external_module.as_deref(), Some("requests"));
    }

    #[test]
    fn leaves_local_python_imports_unresolved() {
        let parsed = parse_source(
            "pkg/main.py",
            r#"
from pkg.utils import helper

def run():
    helper()
"#,
            &[("pkg/utils.py", "def helper():\n    pass\n")],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
        assert!(call.callee_external_module.is_none());
    }

    #[test]
    fn classifies_external_javascript_named_import_calls() {
        let parsed = parse_javascript(
            r#"
import { useState } from "react";

function run() {
  useState();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "useState");
        assert_eq!(call.callee_external_module.as_deref(), Some("react"));
    }

    #[test]
    fn classifies_external_javascript_namespace_member_calls() {
        let parsed = parse_javascript(
            r#"
import * as React from "react";

function run() {
  React.useState();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "useState");
        assert_eq!(call.callee_external_module.as_deref(), Some("react"));
    }

    #[test]
    fn leaves_relative_javascript_imports_unresolved() {
        let parsed = parse_javascript(
            r#"
import { helper } from "./utils";

function run() {
  helper();
}
"#,
            &[
                (
                    "package.json",
                    r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
                ),
                ("src/utils.js", "export function helper() {}\n"),
            ],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn classifies_external_typescript_default_member_calls() {
        let parsed = parse_typescript(
            r#"
import React from "react";

function run() {
  React.useState();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "useState");
        assert_eq!(call.callee_external_module.as_deref(), Some("react"));
    }

    #[test]
    fn leaves_unlisted_javascript_package_aliases_unresolved() {
        let parsed = parse_javascript(
            r#"
import { helper } from "@/utils";

function run() {
  helper();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn classifies_external_go_import_alias_selector_calls() {
        let parsed = parse_go(
            r#"
package main

import (
    "fmt"
    cli "github.com/acme/client"
    "gopkg.in/yaml.v3"
)

func run() {
    fmt.Println("hello")
    cli.Connect()
    yaml.Unmarshal(nil, nil)
}
"#,
            &[("go.mod", "module example.com/app\n")],
        );

        let fmt_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "Println")
            .expect("fmt call");
        assert_eq!(fmt_call.callee_target_kind.as_str(), "external");
        assert_eq!(fmt_call.callee_external_module.as_deref(), Some("fmt"));

        let alias_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "Connect")
            .expect("alias call");
        assert_eq!(alias_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            alias_call.callee_external_module.as_deref(),
            Some("github.com/acme/client")
        );

        let yaml_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "Unmarshal")
            .expect("yaml call");
        assert_eq!(yaml_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            yaml_call.callee_external_module.as_deref(),
            Some("gopkg.in/yaml.v3")
        );
    }

    #[test]
    fn leaves_self_module_go_imports_unresolved() {
        let parsed = parse_go(
            r#"
package main

import "example.com/app/pkg/tool"

func run() {
    tool.Run()
}
"#,
            &[("go.mod", "module example.com/app\n")],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn leaves_unimported_go_selector_calls_unresolved() {
        let parsed = parse_go(
            r#"
package main

func run(client Client) {
    client.Do()
}
"#,
            &[("go.mod", "module example.com/app\n")],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn classifies_external_rust_use_alias_and_path_calls() {
        let parsed = parse_rust(
            r#"
use serde_json::from_str as parse_json;
use std::fs;

fn run() {
    parse_json("{}");
    serde_json::from_str("{}");
    fs::read("Cargo.toml");
    std::fs::read("Cargo.toml");
}
"#,
            &[(
                "Cargo.toml",
                r#"[package]
name = "app"

[dependencies]
serde_json = { version = "1" }
"#,
            )],
        );

        let parse_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "from_str")
            .expect("from_str call");
        assert_eq!(parse_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            parse_call.callee_external_module.as_deref(),
            Some("serde_json")
        );

        let read_modules: Vec<_> = parsed
            .calls
            .iter()
            .filter(|call| call.callee_name == "read")
            .map(|call| call.callee_external_module.as_deref())
            .collect();
        assert_eq!(read_modules, vec![Some("std::fs"), Some("std::fs")]);
    }

    #[test]
    fn leaves_rust_self_crate_and_glob_imports_unresolved() {
        let parsed = parse_rust(
            r#"
use app::helper;
use serde_json::*;

fn run() {
    helper();
    from_str("{}");
}
"#,
            &[(
                "Cargo.toml",
                r#"[package]
name = "app"

[dependencies]
serde_json = "1"
"#,
            )],
        );

        assert_eq!(parsed.calls.len(), 2);
        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn leaves_rust_receiver_method_calls_unresolved() {
        let parsed = parse_rust(
            r#"
fn run(value: Parser) {
    value.parse();
}
"#,
            &[(
                "Cargo.toml",
                r#"[package]
name = "app"
"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn classifies_external_java_import_and_static_import_calls() {
        let parsed = parse_java(
            r#"
package app;

import java.util.Collections;
import static java.util.Objects.requireNonNull;

class Sample {
    void run() {
        Collections.emptyList();
        requireNonNull("x");
    }
}
"#,
            &[],
        );

        let class_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "emptyList")
            .expect("class call");
        assert_eq!(class_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            class_call.callee_external_module.as_deref(),
            Some("java.util.Collections")
        );

        let static_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "requireNonNull")
            .expect("static call");
        assert_eq!(static_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            static_call.callee_external_module.as_deref(),
            Some("java.util.Objects")
        );
    }

    #[test]
    fn leaves_java_wildcard_and_instance_calls_unresolved() {
        let parsed = parse_java(
            r#"
package app;

import java.util.*;

class Sample {
    void run(java.util.List<String> list) {
        list.add("x");
        emptyList();
    }
}
"#,
            &[],
        );

        assert_eq!(parsed.calls.len(), 2);
        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn leaves_local_java_imports_unresolved() {
        let parsed = parse_java(
            r#"
package app;

import app.helpers.Helper;

class Sample {
    void run() {
        Helper.render();
    }
}
"#,
            &[(
                "src/main/java/app/helpers/Helper.java",
                r#"
package app.helpers;

class Helper {
    static void render() {}
}
"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn classifies_external_csharp_alias_static_and_qualified_calls() {
        let parsed = parse_csharp(
            r#"
using Json = Newtonsoft.Json.JsonConvert;
using static System.Math;
using System;

class Sample {
    void Run() {
        Json.SerializeObject(this);
        Sqrt(4);
        System.Console.WriteLine("x");
    }
}
"#,
            &[],
        );

        let alias_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "SerializeObject")
            .expect("alias call");
        assert_eq!(alias_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            alias_call.callee_external_module.as_deref(),
            Some("Newtonsoft.Json.JsonConvert")
        );

        let static_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "Sqrt")
            .expect("static call");
        assert_eq!(static_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            static_call.callee_external_module.as_deref(),
            Some("System.Math")
        );

        let qualified_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "WriteLine")
            .expect("qualified call");
        assert_eq!(qualified_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            qualified_call.callee_external_module.as_deref(),
            Some("System.Console")
        );
    }

    #[test]
    fn leaves_csharp_instance_and_local_namespace_calls_unresolved() {
        let parsed = parse_csharp(
            r#"
namespace App;

using App.Helpers;

class Sample {
    void Run(Client client) {
        client.Send();
        App.Helpers.Tool.Render();
    }
}
"#,
            &[],
        );

        assert_eq!(parsed.calls.len(), 2);
        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn classifies_external_php_namespace_and_fully_qualified_calls() {
        let parsed = parse_php(
            r#"
<?php
namespace App;

use Vendor\Pkg\Client as ApiClient;
use function Vendor\Pkg\do_work as work;

function run() {
    ApiClient::connect();
    work();
    \Vendor\Pkg\helper();
    \Vendor\Pkg\Service::build();
}
"#,
            &[],
        );

        let static_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "connect")
            .expect("static call");
        assert_eq!(static_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            static_call.callee_external_module.as_deref(),
            Some("Vendor\\Pkg\\Client")
        );

        let function_import_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "do_work")
            .expect("function import call");
        assert_eq!(function_import_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            function_import_call.callee_external_module.as_deref(),
            Some("Vendor\\Pkg")
        );

        let qualified_function_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "helper")
            .expect("qualified function call");
        assert_eq!(
            qualified_function_call.callee_target_kind.as_str(),
            "external"
        );
        assert_eq!(
            qualified_function_call.callee_external_module.as_deref(),
            Some("Vendor\\Pkg")
        );

        let qualified_static_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "build")
            .expect("qualified static call");
        assert_eq!(
            qualified_static_call.callee_target_kind.as_str(),
            "external"
        );
        assert_eq!(
            qualified_static_call.callee_external_module.as_deref(),
            Some("Vendor\\Pkg\\Service")
        );
    }

    #[test]
    fn leaves_php_dynamic_member_and_local_import_calls_unresolved() {
        let parsed = parse_php(
            r#"
<?php
namespace App;

use App\Local\Client;

function run($obj) {
    $obj->connect();
    Client::connect();
    \missing();
    missing();
}
"#,
            &[(
                "src/Local/Client.php",
                r#"
<?php
namespace App\Local;

class Client {}
"#,
            )],
        );

        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn classifies_external_ruby_constant_qualified_require_calls() {
        let parsed = parse_ruby(
            r#"
require "json"
require "fileutils"

def run
  JSON.parse("{}")
  FileUtils.mkdir_p("tmp")
  parse("{}")
end
"#,
            &[],
        );

        let json_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "parse")
            .expect("json call");
        assert_eq!(json_call.callee_target_kind.as_str(), "external");
        assert_eq!(json_call.callee_external_module.as_deref(), Some("json"));

        let mkdir_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "mkdir_p")
            .expect("fileutils call");
        assert_eq!(mkdir_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            mkdir_call.callee_external_module.as_deref(),
            Some("fileutils")
        );
    }

    #[test]
    fn leaves_ruby_local_constant_collision_and_receivers_unresolved() {
        let parsed = parse_ruby(
            r#"
require "json"

def run(client)
  JSON.parse("{}")
  client.parse("{}")
  send(:parse, "{}")
end
"#,
            &[(
                "lib/json.rb",
                r#"
module JSON
end
"#,
            )],
        );

        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn classifies_external_dart_alias_calls_only() {
        let parsed = parse_dart(
            r#"
import 'dart:convert' as convert;
import 'package:http/http.dart' as http show Client;
import 'package:app/local.dart' as local;
import './relative.dart' as relative;

void run() {
  convert.jsonDecode("{}");
  http.Client();
  local.helper();
  relative.helper();
  jsonDecode("{}");
}
"#,
            &[(
                "pubspec.yaml",
                r#"
name: app
dependencies:
  http: ^1.0.0
"#,
            )],
        );

        let json_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "jsonDecode")
            .expect("jsonDecode call");
        assert_eq!(json_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            json_call.callee_external_module.as_deref(),
            Some("dart:convert")
        );

        let client_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "Client")
            .expect("Client call");
        assert_eq!(client_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            client_call.callee_external_module.as_deref(),
            Some("package:http/http.dart")
        );

        let unresolved: Vec<_> = parsed
            .calls
            .iter()
            .filter(|call| matches!(call.callee_name.as_str(), "helper" | "jsonDecode"))
            .filter(|call| call.callee_target_kind.as_str() == "unresolved")
            .collect();
        assert_eq!(unresolved.len(), 3);
        assert!(parsed.calls.iter().all(|call| call.callee_name != "run"));
    }

    #[test]
    fn classifies_external_elixir_remote_alias_and_required_calls() {
        let parsed = parse_elixir(
            r#"
defmodule App.Sample do
  alias HTTPoison, as: HTTP
  require Jason

  def run(body) do
    Jason.decode!(body)
    HTTP.get("https://example.com")
  end
end
"#,
            &[
                (
                    "mix.exs",
                    r#"
defmodule App.MixProject do
  defp deps do
    [
      {:jason, "~> 1.4"},
      {:httpoison, "~> 2.0"}
    ]
  end
end
"#,
                ),
                (
                    "mix.lock",
                    r#"{"jason": {:hex, :jason}, "httpoison": {:hex, :httpoison}}"#,
                ),
            ],
        );

        let decode_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "decode!")
            .expect("decode call");
        assert_eq!(decode_call.callee_target_kind.as_str(), "external");
        assert_eq!(decode_call.callee_external_module.as_deref(), Some("Jason"));

        let get_call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "get")
            .expect("get call");
        assert_eq!(get_call.callee_target_kind.as_str(), "external");
        assert_eq!(
            get_call.callee_external_module.as_deref(),
            Some("HTTPoison")
        );
    }

    #[test]
    fn leaves_elixir_local_module_collision_and_imported_calls_unresolved() {
        let parsed = parse_elixir(
            r#"
defmodule App.Sample do
  import Jason

  def run(body) do
    Jason.decode!(body)
    decode!(body)
  end
end
"#,
            &[
                ("mix.exs", "{:jason, \"~> 1.4\"}\n"),
                (
                    "lib/jason.ex",
                    r#"
defmodule Jason do
end
"#,
                ),
            ],
        );

        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn extracts_kotlin_symbols_imports_and_calls_without_external_classification() {
        let parsed = parse_kotlin(
            r#"
package app

import kotlinx.coroutines.runBlocking

class Runner {
    fun run() {
        runBlocking()
        println("hello")
    }
}
"#,
            &[],
        );

        assert!(
            parsed
                .symbols
                .iter()
                .any(|symbol| symbol.name == "Runner" && symbol.kind == "class")
        );
        assert!(
            parsed
                .symbols
                .iter()
                .any(|symbol| symbol.name == "run" && symbol.kind == "method")
        );
        assert!(
            parsed
                .imports
                .iter()
                .any(|import| import.module_name == "import kotlinx.coroutines.runBlocking")
        );
        assert!(
            parsed
                .calls
                .iter()
                .any(|call| call.callee_name == "runBlocking"
                    && call.callee_target_kind.as_str() == "unresolved")
        );
    }

    #[test]
    fn semantic_resolver_can_classify_cpp_calls_as_external() {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        let path = root.join("src/main.cpp");
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
        fs::write(
            &path,
            r#"
void run() {
    printf("x");
}
"#,
        )
        .expect("write source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        let mut resolver = FakeSemanticResolver {
            target: Some(crate::index::semantic::SemanticCallTarget {
                callee_name: "printf".to_string(),
                external_module: "/usr/include/stdio.h".to_string(),
            }),
            expected_language: "cpp",
            expected_callee: "printf",
            requests: Vec::new(),
            error: None,
        };
        let parsed =
            parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
                .expect("parse result")
                .expect("parse file");

        let call = parsed.calls.first().expect("printf call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(
            call.callee_external_module.as_deref(),
            Some("/usr/include/stdio.h")
        );
    }

    #[test]
    fn semantic_resolver_can_classify_textual_dart_calls_as_external() {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        let path = root.join("lib/sample.dart");
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
        fs::write(
            &path,
            r#"
void run() {
  Tooltip(message: 'x');
}
"#,
        )
        .expect("write source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        let mut resolver = FakeSemanticResolver {
            target: Some(crate::index::semantic::SemanticCallTarget {
                callee_name: "Tooltip".to_string(),
                external_module: "package:flutter/material.dart".to_string(),
            }),
            expected_language: "dart",
            expected_callee: "Tooltip",
            requests: Vec::new(),
            error: None,
        };
        let parsed =
            parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
                .expect("parse result")
                .expect("parse file");

        let call = parsed
            .calls
            .iter()
            .find(|call| call.callee_name == "Tooltip")
            .expect("Tooltip call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(
            call.callee_external_module.as_deref(),
            Some("package:flutter/material.dart")
        );
        assert!(resolver.requests.iter().any(|request| {
            request.language == "dart"
                && request.file_path == path
                && request.root_path == root
                && request.callee_name == "Tooltip"
        }));
    }

    #[test]
    fn semantic_resolver_receives_utf16_columns_for_ast_calls() {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        let path = root.join("src/main.cpp");
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
        let source = format!(
            "void run() {{\n    auto s = \"{}\"; printf(\"x\");\n}}\n",
            '\u{1F600}'
        );
        fs::write(&path, source.as_bytes()).expect("write source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        let mut resolver = FakeSemanticResolver {
            target: None,
            expected_language: "cpp",
            expected_callee: "printf",
            requests: Vec::new(),
            error: None,
        };

        parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
            .expect("parse result")
            .expect("parse file");

        let request = resolver
            .requests
            .iter()
            .find(|request| request.callee_name == "printf")
            .expect("printf semantic request");
        let prefix = format!("    auto s = \"{}\"; ", '\u{1F600}');
        assert_eq!(request.line, 2);
        assert_eq!(request.column, prefix.encode_utf16().count());
    }

    #[test]
    fn semantic_resolver_receives_utf16_columns_for_textual_dart_calls() {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        let path = root.join("lib/sample.dart");
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
        let source = format!(
            "void run() {{\n  final s = '{}'; Tooltip(message: 'x');\n}}\n",
            '\u{1F600}'
        );
        fs::write(&path, source.as_bytes()).expect("write source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        let mut resolver = FakeSemanticResolver {
            target: None,
            expected_language: "dart",
            expected_callee: "Tooltip",
            requests: Vec::new(),
            error: None,
        };

        parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
            .expect("parse result")
            .expect("parse file");

        let request = resolver
            .requests
            .iter()
            .find(|request| request.callee_name == "Tooltip")
            .expect("Tooltip semantic request");
        let prefix = format!("  final s = '{}'; ", '\u{1F600}');
        assert_eq!(request.line, 2);
        assert_eq!(request.column, prefix.encode_utf16().count());
    }

    #[test]
    fn semantic_resolver_errors_are_propagated() {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        let path = root.join("src/main.cpp");
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
        fs::write(
            &path,
            r#"
void run() {
    printf("x");
}
"#,
        )
        .expect("write source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        let mut resolver = FakeSemanticResolver {
            target: None,
            expected_language: "cpp",
            expected_callee: "printf",
            requests: Vec::new(),
            error: Some("semantic resolver failed"),
        };

        let err =
            match parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
            {
                Err(err) => err,
                Ok(_) => panic!("expected semantic resolver error"),
            };

        assert_eq!(err.to_string(), "semantic resolver failed");
    }

    #[test]
    fn classifies_external_swift_module_qualified_calls() {
        let parsed = parse_swift(
            r#"
import Foundation

func run() {
    Foundation.Date()
}
"#,
            &[],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "Date");
        assert_eq!(call.callee_external_module.as_deref(), Some("Foundation"));
    }

    #[test]
    fn leaves_swift_unqualified_and_member_calls_unresolved() {
        let parsed = parse_swift(
            r#"
import Foundation

func run(date: Date) {
    Date()
    date.formatted()
}
"#,
            &[],
        );

        assert_eq!(parsed.calls.len(), 2);
        assert!(
            parsed
                .calls
                .iter()
                .all(|call| call.callee_target_kind.as_str() == "unresolved")
        );
    }
}
